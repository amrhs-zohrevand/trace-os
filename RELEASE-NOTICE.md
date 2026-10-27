# Candidate status

This is release candidate 0.1.0-rc25 of TRACE-OS, prepared on 2026-10-27 from its development repository.
TRACE-OS was called TRACEware in 0.1.0-rc19 and the TRACE kit (`trace-kit`) up to 0.1.0-rc18.
The release record retains the archive digest and validation results.
The kit supplies record formats and commands for all five governing actions. It validates trace records and check rows at promotion and in the workflow check, and validates registries before reporting their contents. It distinguishes review, acceptance, return and refusal, retains failed rechecks, and reports which governing-action records are present, missing, not applicable or unknown. These checks establish neither that every action occurred nor that a recorded action was adequate, independent or authorized in fact.
The export manifest records source files and digests.
TRACE-OS is distributed under a perpetual, non-exclusive licence with all other rights reserved, and has not yet been released as open source, because its paper is in preparation for submission and review; the authors will release it under an open-source licence. The licence's full text is `LICENSE` at the archive's root; setup does not copy it into a project, because a project's own licence is its owner's choice.
No third-party fonts, journal classes, data, research prompts or Git history are included.
Document skeletons refer to packages supplied by the user's TeX installation; those packages retain their own terms and are not bundled here.

The release gate is the kit's local rehearsal (`starter/rehearse.py`), a migration rehearsal (an install of 0.1.0-rc24, from its archive, updated to this release, rolled back and updated again), the test suites and the export compared twice, all run on one machine, macOS on Apple silicon with Python 3.12.
This candidate changes no script, so the Linux rehearsal and the Windows run recorded for 0.1.0-rc24's checks were not repeated on this archive: the Linux rehearsal passed (arm64, Python 3.12 and 3.11, with the cheap checks on amd64) and the Windows run did not pass (Limits, below).
Their results are recorded with the release's evidence under sources/coding/trace-os-rc25-20261027/ (this cut), sources/coding/trace-os-rc25-20261022/ (the first, unpublished cut) and sources/coding/trace-os-rc24-20261016/.
Another researcher's setup trial is still pending and runs independently: a release does not wait for it.
A passing mechanical check is not certification of research quality, model independence, or human acceptance.

## Changes in 0.1.0-rc25

TRACE-OS now says how to cite it and states where its licence stands.
Its tools, records and instructions are those of 0.1.0-rc24; records, registries and ledger rows written by an earlier release keep their meaning.

### How to cite

- `CITATION.cff` at the archive's root names TRACE-OS, its repository and website, and the paper that introduces the principles.
- `README.md` gains a How to cite section: the reference, a BibTeX entry, and an example methods sentence that names the release used, which is the `version` in `.trace-os/install.json`.
- Like `LICENSE`, `CITATION.cff` is a release file: setup does not copy it into a project.
- `CITATION.cff`, `LICENSE` and `README.md` write the second author's name as Aldona Kapacinskaite, in ASCII, at her request of 24 October 2026 for computer readability. 0.1.0-rc24 wrote it with diacritics.

### Where the licence stands

- `README.md`, `SETUP-AGENT.md` and the status paragraph above say that TRACE-OS has not yet been released as open source, because its paper is in preparation for submission and review, and that the authors will release it under an open-source licence.
- `LICENSE` itself is unchanged from 0.1.0-rc24: a perpetual, non-exclusive licence to install and use TRACE-OS in one's own research, teaching and evaluation and to share it with the people one works with, with all other rights reserved. Making it public, in a public repository or as part of a public project, still needs the authors' written permission.

### Release

- The supported pair 0.1.0-rc24 to 0.1.0-rc25 is nominated, with unchanged profiles, agent adapters and schemas, and the rc24 archive joins the pinned `releases` list with the digest its release page published (`66be4630...5dc8`). No managed instruction section changes its version.

### Updating from 0.1.0-rc24, or from an earlier candidate

- A project on 0.1.0-rc24 updates in one step with this archive, as any single update does; the update changes `SETUP-AGENT.md` and `RELEASE-NOTICE.md`.
- A project's `README.md` is the project's own once setup seeds it, so the How to cite section and the licence note reach the README of a new project only; a project set up by an earlier candidate keeps the README that candidate wrote, and its person may add them.
- A project on 0.1.0-rc23 or earlier updates through the chain (`--update-chain`), which applies rc24 on the way. The releases page carries 0.1.0-rc24 and later; the archives a chain from rc23 or before needs come from a folder named with `--release-store`, as the notice for 0.1.0-rc24 says below.

### Limits

- This candidate changes text, `CITATION.cff` and the export manifest; no script changed. Its checks are the release checks of 0.1.0-rc24 run again on this cut, on one macOS machine. No model review was run on it, and no person has accepted the work.
- A first cut of this candidate was made on 22 October 2026 from an earlier commit (archive sha256 `17ff1ebe...60f7`, checks under sources/coding/trace-os-rc25-20261022/). It was never published and is superseded by this cut, which differs from it only in the spelling of the second author's name in `CITATION.cff`, `LICENSE` and `README.md`.
- On Windows, the kit's own test suite fails 146 of 1,325 tests on Windows Server 2025 with Python 3.12 and 3.11 (the run of 2026-10-22 on 0.1.0-rc24's archive): most of them in the tests' own removal of temporary Git repositories, the rest in path separators, a lock-folder move, text encoding and line endings. Because the release preflight runs that suite, an update on Windows stops at the preflight. The seven defects are AH-359 to AH-365, open; AH-282 and AH-283, on the entry-by-entry move that Windows falls back to, were open before. Export, digests, terminology, build configuration and the defect ledger pass on Windows.
- AH-354 (the note a rehearsal run lost) and AH-357 (an automatic exclusion `keep` keeps) remain open, as in 0.1.0-rc24.
- The licence was drafted by a model from the arXiv licence, as 0.1.0-rc24's notice says, and has not been read by a lawyer.

## Changes in 0.1.0-rc24

TRACE-OS now carries the licence its paper carries on arXiv, describes itself as the paper does, and is published from a new repository whose history begins with this release.
Its tools, records and instructions are those of 0.1.0-rc23; records, registries and ledger rows written by an earlier release keep their meaning.

### The licence

- `LICENSE` is no longer the Apache License 2.0. It is a perpetual, non-exclusive licence to distribute, modelled on the arXiv.org licence under which the paper is posted, and the two authors reserve every other right.
- You may install and use TRACE-OS in your own research, teaching and evaluation, let its setup and updates change your project's files, edit it to configure it for your projects, and share it with the people you work with on those projects. This permission does not end.
- You may not distribute, publish, sell or sublicense TRACE-OS or a version of it, or make it publicly available, in a public repository or as part of a public project for instance, without the authors' written permission.
- The authors keep their copyright and stay free to license or transfer it, to a publisher for instance. The licence claims nothing in the research, records, manuscripts or data you make with TRACE-OS, and it comes without warranty.
- Setup still does not copy `LICENSE` into a project, because a project's own licence is its owner's choice. The export manifest names the licence `LicenseRef-TRACE-OS-Nonexclusive-Distrib`.

### How TRACE-OS describes itself

- `README.md` describes TRACE-OS as the paper does: an operating system for AI-assisted research projects, in which researchers direct the work and AI agents carry it out and record it, putting the five TRACE principles into practice, and it names the paper.

### The distribution repository

- From this release TRACE-OS is published on a new repository, `amrhs-zohrevand/trace-os`, whose history begins at 0.1.0-rc24, with one commit, one tag and one pre-release for each release as before. It stays private until the authors make it public.
- The earlier candidates, 0.1.0-rc1 to rc23, are withdrawn and are not for use or redistribution. They were only ever available to the two authors, and 0.1.0-rc19 to rc23 carried the Apache License 2.0; they stay in a private archive repository, marked withdrawn there.
- `SETUP-AGENT.md` and `scripts/kit_projects.py` say that the releases page carries 0.1.0-rc24 and later, and that a chain from an earlier release takes the earlier archives from a folder named with `--release-store`.

### Release

- The supported pair 0.1.0-rc23 to 0.1.0-rc24 is nominated, with unchanged profiles, agent adapters and schemas, and the rc23 archive joins the pinned `releases` list with the digest its release page published (`0ac1f54b...767e`). No managed instruction section changes its version.

### Updating from 0.1.0-rc23, rc22 or rc21

- A project on 0.1.0-rc23 updates in one step with this archive, as any single update does; the update changes `SETUP-AGENT.md`, `RELEASE-NOTICE.md` and `scripts/kit_projects.py`.
- A project on 0.1.0-rc22 or rc21 updates through the chain (`--update-chain`), which applies rc23, and rc22 before it, on the way. Their archives are not on the releases page: name a folder that holds them with `--release-store`, and each is checked against the digest this release pins, whatever its file name. `kit_projects.py` takes the same `--release-store`, with `--release-archive` or with `--download`. The authors, the only people who had those candidates, keep their archives.
- A section edited under a compatibility declaration for its current version stays compatible, because no section's version changes.
- A project's `README.md` is the project's own once setup seeds it, so an update leaves it as an earlier candidate wrote it, including that candidate's licence sentence; its person may change that line.

### Limits

- The licence was drafted by claude-opus-5-5 from the arXiv licence and has not been read by a lawyer. It names the two authors as the copyright holders, which an employer's rules on work made in employment may change.
- Copies of 0.1.0-rc19 to rc23 that the authors already hold keep the Apache License 2.0 they came with; withdrawing those candidates stops their distribution and does not change the terms of copies already made.
- This candidate changes text and one message of `kit_projects.py`; its checks are the release checks of 0.1.0-rc23 run again on this cut, and no model review was run. No person has accepted the work.
- AH-354 (the note a rehearsal run lost) and AH-357 (an automatic exclusion `keep` keeps) remain open, as in 0.1.0-rc23.
- Only macOS on Apple silicon with Python 3.12 was run; Linux and Windows were not.

## Changes in 0.1.0-rc23

An agent session that commits work keeps a trace record of it, and one command does the record's routine before each commit; where the kit used to leave agents of different providers a judgment, a tool or a stated rule now carries it.
Records, registries and ledger rows written by an earlier release keep their meaning.

### One trace record per agent commit

- The instructions say it outright: a session that commits work keeps a trace record; a commit a person typed owes none.
- `trace_record.py keep --subject "<the commit's subject>"`, run before each commit, finds the running Claude Code or Codex session's own transcript, refreshes the ignored staging folder, writes coverage, screens the files, records the disclosure review, promotes the record to `ai/records/<id>`, verifies it and drafts the commit body with its `Record:` line and `Execution-agent:` trailer. A later commit of the same session updates the same record without renumbering a turn or rewriting a promoted decision or check, and `keep` refuses to overwrite a promoted record whose files changed after promotion.
- `keep` decides nothing that is the agent's: it clears the disclosure review only with `--cleared` and the digest of the captured turns it printed, and only when the local screen finds nothing; it leaves a turn out only with a stated reason, or when the same transcript line is summarized in another promoted record, which it names; it never writes the summary, a decision or a check, and never stages or commits. When something is missing it names it and promotes nothing; nothing it reports stops a commit.
- On a session's first record `keep` names the speaker from `git config user.name`, unless that reads as a model's name, and records the basis; an `--execution-agent` the session did not report is marked self-reported in the record and the body.
- `trace_record.py session` names the running session's transcript and the model that executed it, under Claude Code and Codex alike, and `extract current` extracts it. A refresh keeps the earlier instruction digests, so a check pinned to a skill edited mid-session still promotes.

### Agents of different providers

- `queue` lists the commits that name no trace record, each with its subject and who executed it (the first ten and a count of the rest; `queue --json` carries all), instead of only counting them.
- The shipped instructions state what the kit left to each model's judgment: no em dash anywhere, chat replies included; who the person is (`git config user.name`, unless they named themselves otherwise; ask when another name for the speaker is in view); and what to do when a sandbox refuses a commit.

### Review against sources

- The reviewer's view of a trace record names the commits under review and every commit that names the record, separately, so a reviewer no longer reads a record's other commits as missing.

### Release

- The supported pair 0.1.0-rc22 to 0.1.0-rc23 is nominated, with unchanged profiles, agent adapters and schemas, and the rc22 archive joins the pinned `releases` list with the digest its release page publishes.

### Updating from 0.1.0-rc22

- The update replaces the `core-workflow` section of `AGENTS.md`, now version 6 because it states the trace-record rule. A project that kept that section edited under a compatibility declaration for version 5 has it reported unresolved until the declaration names version 6, so the rule does not pass an edited section silently.
- After the update, `queue` names the commits that belong to no record where it used to count them; nothing in a record changes.

### Limits

- `keep` was rehearsed with Claude Code only (claude-opus-5-5 and claude-sonnet-5-5, three runs each, on the stage 1 build): every run kept a record, against 2 of 6 with 0.1.0-rc22. Codex is covered by tests on synthetic rollouts only; Cline was not run.
- A subagent shares its parent's session, so `keep` reads the parent's transcript; a subagent passes `--execution-agent` with its own model id, which the record then marks self-reported.
- An exclusion `keep` writes for a turn the summary does not yet cite stays after the summary cites it (AH-357, open); remove it from the staged coverage, or cite the turn before the first run.
- Whether a resumed session writes a new transcript file was not verified; if it does, `keep` asks for a new `--record-id`.
- This candidate's release review was two advisory runs of `scripts/agentic_review.py` with claude-fable-5-1 through the claude backend, one on `keep` and one on its instruction text, and one recheck of the fixes by the same model: a different model from the one that executed the work, from the same developer; no model of another developer reviewed it, because this release used the Claude subscription only. The runs' nine findings entered the ledger (AH-344 to AH-352), and the coordinating session and the recheck's notes opened four more (AH-353 to AH-356); all but AH-354 were fixed before the cut, the recheck verified eight, and two stay fixed with the recheck's notes answered by AH-355 and AH-356, which no further run rechecked. No person has accepted the work.
- In the rehearsal one run lost a note it had written and not yet committed, while the workflow check ran in the background; four reproductions did not find the cause and no command in the run removed a file (AH-354, open). Commit work before running long checks.
- Only macOS on Apple silicon with Python 3.12 was run; Linux and Windows were not.

## Changes in 0.1.0-rc22

The kit now does what each of the five principles asks of a research team, as the principles are stated for the OrgSci technical appendix, and names them as verbs: track records, review against sources, accept independently, control transitions, evaluate corrections.
Records, registries and ledger rows written by an earlier release keep their meaning: every new field is optional on old rows, and what an old row lacks reads `not stated` or is reported, never refused.

### The principles as verbs

- The reports, `TRACE-STATS.md` and the shipped skills name the principles as verbs; the skill ids (`trace-state`, `trace-review`, `trace-acceptance`, `trace-ctransition`, `trace-evaluate`, `trace-check`) do not change.

### Track records

- A decision row states its `reason`: why the selected choice was taken over the alternatives. A row without one reads `not stated`; `verify` and `view` count such rows as information, and `brief` prints the reason apart from the human direction the choice answers (which no longer reads as "why").
- A decision row may name its `inputs` (paths, intake rows, earlier records), and `verify` reports where each decision's artifacts and inputs can be found, as information.
- The agent records each choice when it makes it, not at the session's end, re-running `extract` into the same staging folder when the turn that directed it is newer; a refresh keeps the speaker the earlier extraction named.
- `brief` shows a choice's reason in full, and keeps the manuscript apart from the standing instructions. The decision types are listed in `decision --help` and the convention, and the convention says to clear consent before `cover` and how to recover.

### Review against sources

- `agentic_review.py --source PATH` gives the reviewer the files to check against, from the repository, the shared folder or a verified snapshot, each with its digest; a `--path` candidate may sit in the shared folder.
- Every run writes its report, the findings as returned, the prompt and the rows it recorded to `ai/reviews/<name>/`, tracked, so a coauthor on another clone can judge what was examined and found; the report prints `evidence_examined` and the criteria requested beside those applied. The raw transcript stays local, named with its digest. When a source or candidate is one Git ignores, or the records are sent in full, the reviewer's own text stays local too and the tracked report says why.
- Check rows may name their `sources`; a finding may quote the source as well as the reviewed file, and both quotes are labelled.
- `trace_record.py check --person|--agent` records a check from the checker's own words, printed back field by field and appended only once they confirm.
- `brief` shows each check's candidate, criterion, sources, checker and what was examined, and the report says whether R rests on named sources, a criterion only, or an artifact alone. A snapshot named as a source is verified against its manifest, by `check` as by the reviewer.
- The reviewer contract no longer carries one project's prose rules: a project that wants them keeps them in `ai/review-prose-rules.md`, a standing instruction.
- The decision summary an outside reviewer receives includes each decision's stated reason.

### Accept independently

- `brief` names the version it shows ("covers N commits through <sha>"), and `accept --record <id> --through <sha>` binds a decision to it: a commit that lands after the person read the brief is not covered and stays waiting.
- `brief` and `queue` show each record's owners.
- A delegated acceptance stores the authorization's words in `--authority`; without them it reads "authority not stated" and is advisory, as an agentic review run already was.
- A person's decision about commits they typed themselves stays theirs, and the row and `review-status` say they accepted their own work.
- An actor that selected one of the record's choices is not independent of it, like one that executed it.
- A record that logs no choice is labelled `needs you`, never `routine`.

### Control transitions

- `intake` records who decided (`--by` defaults to the Git user, with `--person`, `--agent` or `--authorized-by` declaring the kind), the decision (`--decision admit|return|refuse`) and the material's `--version`.
- Outside material waits for that decision in `incoming/` inside the shared folder (`incoming_root` in `ai/project.json` names another folder), and moves into the project's data only after an admitting row; `shared_folder.py status` names the folder and counts what it holds.
- `WORKFLOW.md` and `ai/review-prose-rules.md` are standing instructions for control transitions, as `AGENTS.md` is.
- The report and the workflow check list data that no admitting intake row names (files added under `sources/`, the shared inventory, snapshots), and a returned or refused file found in the data anyway, as information; material in `incoming/` is counted by decision, not as data.
- A transition is reported with its decision, and a returned or refused object that is nevertheless in `paper/` is flagged.

### Evaluate corrections

- A ledger row may list the outputs a defect reaches (`--object`, on `open`, `mark` and a return through `accept`), and verifies only when each has its own passing recheck; until then it says how many are rechecked and which still wait.
- A row closed by hand is a repair claim and stays in what E is waiting on until a recheck reaches it.
- A later run must name an existing trace record and an instruction version that changes the faulty default, or the recheck is recorded and does not verify.
- `defect_ledger.py ingest --run ai/reviews/<run>` takes an agentic review's findings into the ledger, and every supported finding gets a row before it is fixed.
- The report's per-commit evaluate-corrections state joins a commit that claims a fix of a ledger row ("Fixes AH-001", "Closes ...") to that row's recheck, and never a row a later sweep closed; write the fix claim in the commit that fixes a row.
- A recurrence starts a row's per-object round again, and an object that is not a file (a table, a section) takes a hash the checker states with `draft-recheck --object-hash`.
- The ledger's id prefix is `ledger_prefix` in `ai/project.json`, else the prefix the existing rows carry, else `DEF` for a new project.

### Release

- The supported pair 0.1.0-rc21 to 0.1.0-rc22 is nominated, with unchanged profiles, agent adapters and schemas, and the rc21 archive joins the pinned `releases` list with the digest its release page publishes.

### Updating from 0.1.0-rc21

- The update replaces the `core-workflow` section of `AGENTS.md` (version 5), the `working-routine` section of `WORKFLOW.md` (version 3) and the `skill-routing` section of `ai/SKILLS-IN-USE.md` (version 3). A project that kept one edited under a compatibility declaration for the earlier version has it reported unresolved until the declaration names the new version.
- After the update, `queue` labels more records `needs you` (those that log no choice), the report's evaluate-corrections column counts corrections as well as default adoptions, and intake rows written before this release read "kind not declared". None of this changes a record; it reports what was not recorded.

### Limits

- This candidate's release review was eleven advisory runs of `scripts/agentic_review.py` with claude-fable-5-1 through the claude backend, one per kit commit since 0.1.0-rc21: a different model from the one that executed the work, from the same developer; codex, used for earlier candidates, had run out of quota. Its 27 findings, all quote-verified, were triaged under the release's holding rule; 25 were supported and entered the ledger (AH-299 to AH-323), all fixed before the cut with tests, two of them already fixed by an earlier commit of this candidate. One further run rechecked the two fix commits and raised two low findings in them, AH-324 and AH-325, also fixed before the cut, one with a test; by this candidate's own stopping rule they were not rechecked by another model run. The release checks on the first cut found three more, AH-326 to AH-328, fixed before this cut: the archive's own suite failed outside a Git repository, which stopped every update in its preflight; the kit's rehearsal expected the old ledger prefix and an advisory form of delegation; and one test read the person from the wrong Git setting. None is verified by a recorded recheck, and no person has accepted the work. The reports and the triage are with the release's evidence.
- The five worked examples of the principles were run once in a fresh install of the development tree before the cut; control transitions had a gap (nothing held outside material before a decision), fixed before the cut.
- The evaluate-corrections join reads fix-claim wording in commit messages; a commit that names a row without "Fixes" or a like word is not joined, so projects whose history names rows that way see fewer joins.
- `trace_skills.py` records an adopted skill's intake row with `--by` and no actor kind, which reads "kind not declared".
- A tracked review report still carries the reviewer's own words, check output tails and command text in an ordinary run, which can include absolute local paths.
- Only macOS on Apple silicon with Python 3.12 was run; Linux and Windows were not.

## Changes in 0.1.0-rc21

Every project has one shared place for its large and bulky files, kept out of Git.
The person says once where it is, and the agent does the technical steps.

### The shared folder

- `scripts/shared_folder.py` (standard library only; rclone only for a bucket) connects, checks and records the project's shared folder.
  - `status` says where the shared files are on this computer and what to do next. `find` lists the sync apps' folders in their documented places, each folder once.
  - `connect` links `workspace/shared/` (or `shared_root` in `ai/project.json`) to the folder the person names, and adds `/workspace/shared` to `.gitignore`, because the rule `workspace/shared/` does not match a link. `--plan` writes nothing; `--create`, `--move-existing` and `--replace` cover a new folder, files already in this copy and a folder that moved. A move never replaces anything in the shared folder: a name that appears there during the move stops it, and says what moved so far. `connect` refuses a shared path where Git tracks files, and `shared_root` is always a folder below the project. `disconnect` removes only the link.
  - `bucket set` and `bucket unset` record a bucket's location in `ai/project.json`, without credentials. `pull` and `push` copy through `rclone copy --update`, which deletes nothing on either side and keeps a newer file at the destination, with a plan first.
  - `check` names conflict copies, online-only files, Google Docs shortcuts and temporary files, and changes nothing. `inventory` writes `sources/shared-inventory.json`, each file's path, size and SHA-256, for Git; `verify` compares this computer's copy with it. A digest is read again whenever the file is not the same file (size, modification time, device and inode); an online-only file keeps the digest already recorded while its size is unchanged; and when part of the folder cannot be read, `inventory` leaves the recorded inventory as it was and `check` and `verify` name what they could not read.
  - `snapshot` takes a verified local copy under `workspace/snapshots/` before an analysis whose results will be reported. `keep-local` keeps a path out of Git and names the files Git already tracks under the same rule, wherever they sit.
- `AGENTS.md` (section `core-workflow`, version 4) has the agent run `status` before work that adds or reads bulky files, ask the person where the folder is when it is not connected, decide where each file goes, verify the inventory after a pull and rewrite it after a change, and snapshot before a reported analysis.
  `WORKFLOW.md` (section `working-routine`, version 2) names the shared folder in the routine, and `SETUP-AGENT.md` has a section Shared folder; setup's Check and report step ends by connecting it.
- The workflow check reports the shared folder's state on this computer as information, without the folder's path, and never fails on it; `TRACE-STATS.md` shows it.
- `tests/test_shared_folder.py` has 38 tests; five run rclone's local backend and skip where rclone is absent.

### Release

- The supported pair 0.1.0-rc20 to 0.1.0-rc21 is nominated, with unchanged profiles, agent adapters and schemas, and the rc20 archive joins the pinned `releases` list with the digest its release page publishes.

### Updating from 0.1.0-rc20

- The update replaces the `core-workflow` section of `AGENTS.md` and the `working-routine` section of `WORKFLOW.md`, whose versions rose.
  A project that kept either section edited under a compatibility declaration for the earlier version has it reported unresolved, and the update stays pending until the declaration names the new version.
- After the update, ask the agent to connect the shared folder. A project that keeps bulky files in a plain `workspace/shared/` folder is reported as `local only`, and `connect --move-existing` moves them into the team's folder once the person agrees.

### Limits

- The Windows paths (a directory junction without Developer Mode, the OneDrive variables, the online-only file attribute) are simulated in tests and were not run on Windows.
- On Linux, a coauthor in a OneDrive or Google Drive team makes the team's folder local with rclone (`rclone mount`, or `rclone bisync`) and connects it; that path is described and was not run. A bucket is the whole project's route, recorded for every coauthor.
- On macOS, `find`, `connect`, `check`, `inventory`, `verify`, `keep-local` and `disconnect` ran once on one machine against real OneDrive and Google Drive folders, without writing to them; no real bucket was used, only rclone's local backend.
- An online-only file is compared by size alone unless `--download` is given, because reading it downloads it.
- The hint that the project itself sits in a synced folder is read from folder names, and is a hint, not a verdict.
- `check` counts a file named like a sibling with " (1)" added as a conflict copy, so a copy a person made on purpose is named too; it deletes nothing either way.
- This candidate's review (two codex runs of `scripts/agentic_review.py`, model review) and the run on macOS raised eight findings in the shared folder, AH-287 to AH-294, all fixed before the cut. One further codex run rechecked the fix and raised three more in it, AH-295 to AH-297, also fixed before the cut, with tests; by this candidate's own stopping rule they were not rechecked by another model run. The reports are with the release's evidence. None is verified by a recorded recheck, and no person has accepted the work.
- On a file system with neither an exclusive rename nor hard links, `connect --move-existing` stops rather than risk replacing a file, and says what moved so far.
- The skill-scenario tests can still fail once with `File exists` when Git's background maintenance locks their cached fixture; run them again (AH-284).

## Changes in 0.1.0-rc20

The kit has a new name, TRACE-OS.
As in 0.1.0-rc19, the name changes everywhere the kit names itself, its folders and its variables included, and every earlier name is still read, so a project installed with an earlier release keeps working until it is updated, and the update moves it.
TRACE itself keeps its name: the five governing actions, the trace record, the `trace-*` skills and the `trace_*.py` scripts.

### The name

- The distribution repository is <https://github.com/amrhs-zohrevand/trace-os> (it was `traceware`, and `trace-kit-starter` before that), and the development and paper repository <https://github.com/amrhs-zohrevand/trace-os-dev> (it was `traceware-dev`, and `trace-kit`); GitHub redirects the earlier names.
- The release asset is `trace-os-0.1.0-rc20.zip` with `trace-os-0.1.0-rc20.zip.sha256`, in the same `digest  name` format, and the archive's files sit under one top folder, `trace-os/`.
  Earlier releases keep the names they were published under, and their archives, under `traceware/` (0.1.0-rc19) or `trace-kit-starter/` (up to 0.1.0-rc18), are still applied by a chain.
  A `kit_projects.py` from 0.1.0-rc19 or earlier cannot read the new archive, so run the one this release ships.
- A project's installation state is in `.trace-os/`.
  Where 0.1.0-rc19 knew one earlier name, every tool now holds an ordered list, newest first: `.traceware/`, then `.trace-kit/`, and the same for the archive's top folder, the section markers, the variables and the `ai/project.json` key.
  An update moves the state to the folder its own release's manifest names, and only to a newer name: the hop to 0.1.0-rc19 moves `.trace-kit/` to `.traceware/`, and the update to this release moves either earlier folder to `.trace-os/`, whole, once it is final.
  A chain from 0.1.0-rc18 or earlier therefore moves the folder twice, and a project on 0.1.0-rc19 once; `--rollback` steps back one hop at a time and moves the folder back with it.
- A path the update history, the skill tool's lock or a receipt recorded under either earlier folder is read where the file is now, after one move or two (AH-281's three tests now run for a lock written in `.trace-kit/`, in `.traceware/`, and across both moves).
- The managed instruction sections open and close with `<!-- trace-os:begin <id> -->` and `<!-- trace-os:end <id> -->`; the update converts either earlier form, and a section kept under a compatibility declaration keeps its text and changes only its two marker lines, and since no section's version changed, its declaration still holds.
  The receipt says which markers became which.
- The starter's `.gitignore` names `.trace-os/`.
  Where the project edited it, the update keeps every line and adds each `.trace-os/` rule after the earlier folder's rule, once where both earlier folders have it.
- The variables are `TRACE_OS_SHARED_ROOT`, `TRACE_OS_REQUIRE_SHARED` and `TRACE_OS_DEFECT_LEDGER`; while one is unset, its `TRACEWARE_` name and then its `TRACE_KIT_` name still work, and the suite in `--shared` mode is told under all three names.
- The compatibility declarations in `ai/project.json` sit under the key `trace_os`; one written under `traceware` or `trace_kit` still holds while the file has no newer key, and no update rewrites the file.
- The temporary folders the updater and `kit_projects.py` make are named `trace-os-*`.

### Release

- The supported pair 0.1.0-rc19 to 0.1.0-rc20 is nominated, with unchanged profiles, agent adapters and schemas, and the rc19 archive joins the pinned `releases` list with the digest its release page publishes.

### Limits

- After the update to this release, a skills operation makes that update's `--rollback` refuse, as after 0.1.0-rc19: the update rewrites the routing block of `ai/SKILLS-IN-USE.md` in every project, since it converts the block's markers.
  Roll back first, then adopt.
- Where an edited `.gitignore` names both earlier folders, the `.trace-os/` rule lands after the first of them, so the three rules read `.trace-kit`, `.trace-os`, `.traceware`; every line is kept.
- On Windows a folder holding an open file cannot be renamed, so there the state folder's entries move one by one; that path is simulated in tests and was not run on Windows, and AH-282 and AH-283, the two open findings about it, still hold.
  A move that cannot be made leaves this release installed in the earlier folder, where every tool still reads it, until `--recover` or the next writing command makes it.
- Paths written into the registries before a move, such as an intake row naming a receipt under `.traceware/skill-adoptions/`, are history and keep their names.
- The skill-scenario tests can still fail once with `File exists` in `test_trace_skills_scenarios` when Git's background maintenance locks their cached fixture; run them again (AH-284).
- This candidate's review found one false receipt line, fixed before the cut (AH-285); the review's quote check had dropped that finding, because its quote spanned two source lines, and that check is unchanged (AH-286).

## Changes in 0.1.0-rc19

The kit has a new name, TRACEware, and a licence, the Apache License 2.0.
The name changes everywhere the kit names itself, its folders and its variables included, and every old name is still read, so a project installed with an earlier release keeps working until it is updated, and the update moves it.
TRACE itself keeps its name: the five governing actions, the trace record, the `trace-*` skills and the `trace_*.py` scripts.

### The name

- The distribution repository is <https://github.com/amrhs-zohrevand/traceware> (it was `trace-kit-starter`), and the development and paper repository <https://github.com/amrhs-zohrevand/traceware-dev> (it was `trace-kit`).
- The release asset is `traceware-0.1.0-rc19.zip` with `traceware-0.1.0-rc19.zip.sha256`, in the same `digest  name` format, and the archive's files sit under one top folder, `traceware/`.
  Earlier releases keep the names they were published under, and their archives, under `trace-kit-starter/`, are still applied by a chain.
  In every update path the new release's own updater reads the new archive, which is why the archive could take the new name; a `kit_projects.py` from 0.1.0-rc17 or 0.1.0-rc18 cannot read it, so run the one this release ships.
- A project's installation state is in `.traceware/`, which was `.trace-kit/`.
  The update to this release, alone or as the last hop of a chain, runs and checks as any update does and, once final, renames the old folder whole: `install.json`, `update-history.jsonl` with the update's event, the receipts, the skill tool's lock, receipts and originals, and the local backups.
  The receipt's remaining steps say what moved and to stage both folders, so Git records the tracked state as renamed; `--rollback` moves the folder back first and restores the rest.
  A hop to an earlier release leaves the folder where that release's own tools read it.
- Every tool reads `.traceware/` first and `.trace-kit/` after it: the updater, `kit_projects.py`, which finds projects in either, `check_workflow.py`, `check_skills.py`, `trace_skills.py` and the rest.
  A path the update history, the skill tool's lock or a receipt recorded under `.trace-kit/` is read where the file is now.
- The managed instruction sections in `AGENTS.md`, `ai/SKILLS-IN-USE.md` and `WORKFLOW.md` open and close with `<!-- traceware:begin <id> -->` and `<!-- traceware:end <id> -->`.
  The update converts the `trace-kit:` markers an earlier release installed; a section kept under a compatibility declaration keeps its text and changes only its two marker lines, and since no section's version changed, its declaration still holds.
- The starter's `.gitignore` names `.traceware/`.
  The update replaces the kit's lines where the project left the file as installed, and where the project edited it, keeps every line and adds the `.traceware/` rule after each `.trace-kit/` rule.
- `check_workflow.py --shared` reads `TRACEWARE_SHARED_ROOT` and `defect_ledger.py` reads `TRACEWARE_DEFECT_LEDGER`; each `TRACE_KIT_` name still works while its new name is unset, and the suite in `--shared` mode is told under both names, `TRACEWARE_REQUIRE_SHARED` and `TRACE_KIT_REQUIRE_SHARED`.
- The compatibility declarations in `ai/project.json` sit under the key `traceware`; one written under `trace_kit` still holds while the file has no `traceware` key, and no update rewrites the file.
- `kit_projects.py --download` fetches a release's one archive by pattern, whatever name it was published under, and the temporary folders the updater and `kit_projects.py` make are named `traceware-*`.

### Licence

- TRACEware is licensed under the Apache License 2.0.
  `LICENSE`, its full and unmodified text, sits at the archive's root; the manifest lists it with the policy `release`, which no setup or update installs, and records `license: Apache-2.0` for the release and each file.
- The earlier release bodies' line that a candidate was not for redistribution or public posting no longer applies.

### Other changes since 0.1.0-rc18

- `trace_record.py extract` captures the answers a person gives through an agent's question dialog, as a turn of its own kind that marks the agent's question and options as the agent's and the choice or the typed words as the person's (AH-274).
  An answer that opens with offered labels and goes on in typed words is recorded as such, and the registries' words lookup reads only the person's answers and notes in such a turn (AH-279, AH-280).
  Records promoted before are not extracted again; the fix works forward, and `ai/RECORD-CONVENTION.md` describes the dialog turns.
- `extract` skips a transcript row it has already read, which a Claude Code transcript can repeat after a compaction, instead of numbering it as a turn of its own; a record extracted before keeps its turn ids when it is refreshed (AH-229).
- A release preflight that refuses an update keeps its output in the ignored receipts folder and names the failed tests (AH-250).
- `trace_skills.py`: the advice to move a template skill's `scripts/<name>/` into its package says to change the skill's commands in the same step (AH-259); `declare --beside-project-skill` records a personal skill beside the project's own skill of that name, which stays as it is (AH-260); `link` on a receipt that kept an existing copy says the copy was kept and whether the commit changed its `SKILL.md` (AH-261).
- The agentic reviewer labels a check run with a trailing `2>&1`, `>&2` or `>/dev/null` by the check's own exit status, which such a redirect leaves unchanged (AH-262), and its outside-checkout and modified-paths sections say `None.` when they list nothing (AH-263).
- The `literature-processing` example's coding record names the coder with a placeholder instead of a model id (AH-270).

### Release

- The supported pair 0.1.0-rc18 to 0.1.0-rc19 is nominated, with unchanged profiles, agent adapters and schemas, and the rc18 archive joins the pinned `releases` list with the digest its release page publishes.

### Limits

- After the update to this release, a skills operation makes that update's `--rollback` refuse: the update rewrites the routing block of `ai/SKILLS-IN-USE.md` in every project, since it converts the block's markers, and a skills operation rewrites that file, and `.traceware/install.json` when it writes agent copies.
  Roll back first, then adopt.
- On Windows a folder holding an open file cannot be renamed, so there the state folder's entries move one by one and the setup lock's file goes once released; that path is simulated in tests and was not run on Windows.
  A move that cannot be made leaves this release installed in `.trace-kit/`, where every tool still reads it, until `--recover` or the next writing command makes it.
- Paths written into the registries before the move, such as an intake row naming a receipt under `.trace-kit/skill-adoptions/`, are history and keep their names.
- The checks ran on macOS only; the Windows paths are simulated in tests, as in rc18.
- Two findings of this candidate's review concern that entry-by-entry move and are open: a rename failing after the receipts have moved lets the final receipt recreate `.trace-kit/receipts`, and `--recover` then refuses on the two folders, without losing anything (AH-282); and while the setup lock's file waits in the old folder, a second writer could take the new folder's lock (AH-283). Neither arises where the folder moves whole.
- The skill-scenario tests copy a cached fixture repository, and Git's background maintenance can lock it mid-copy; the failure reads `File exists` in `test_trace_skills_scenarios`, and since an update's release preflight runs the release's tests, it can refuse an update once. Run it again (AH-284).

## Changes in 0.1.0-rc18

This candidate adds one tool, for skills that come from outside the kit: a project can take a skill package from any Git repository, see all of it before anything is written, keep the untouched original beside its own copy, update it later with the same review, and record each step where co-authors and the trace record can see it. The rest of the kit changes only where the tool needs it, as listed below, and in one example's front matter.

### Skills from a source

- `scripts/trace_skills.py` has nine commands. `list` and `show` print what a source holds and write nothing in the project; `adopt` brings one package in; `status` says where every skill stands; `update` reviews a new upstream like a first adoption, with `--accept-merged` to record a merge a person made by hand and `--detach` and `--attach` to stop and resume updates; `retire` removes a copy, keeping its original unless `--drop-base`; `declare` records a skill a person runs from their own setup, copying nothing; `link` writes the default-adoption transition row once the person has committed; `recover` completes or undoes an interrupted operation. `SETUP-AGENT.md`, Skills from a source, says how, and the starter's `AGENTS.md` sends an agent there.
- `scripts/trace-skills-contract.md` (contract version 1) is the interface the tool and its tests were built against: the lock, the receipt, the candidate file, the package digest, the status vocabulary, every message and exit code, and the transaction and recovery rules. Its section 14 records the open questions and the answers given to them.
- The tool keeps the project's copy under `.skills/<name>/`, the untouched original under `.trace-kit/skill-bases/`, a lock in `.trace-kit/skills.lock.json` and one receipt per operation under `.trace-kit/skill-adoptions/`, and writes the agent copies for the installed adapter. Each operation is one transaction under the setup lock, with the receipt written last and an operation journal kept under `.trace-kit/receipts/` while it writes.
- It never runs, imports or checks out a file from a package, never merges an adapted copy, and never commits or pushes. Nothing is applied without a yes at the terminal, or `--yes`, which an agent passes only once the person has agreed and which applies only the bytes the person was shown.
- Copying a skill from a source not known to be public into the project needs `--share-with-project`, with the person's words; the receipt records both audiences and who decided.
- A name the kit ships is replaced only with `--replace-kit-copy`, and a folder the project already holds is connected to a package only with `--keep-existing`.
- `adopt`, `update` and `--accept-merged` write an intake row naming the receipt; `link` writes the transition row after the commit, with the committed `SKILL.md` and its digest, so later-run discovery can find records made under the skill.
- Each package is reported against definition 1: three states, each on its own (the package format, a stated licence, a stated handoff to the record), with a limit line saying that it reads the skill's text and establishes neither that a method is valid, that its scripts are safe, nor that any agent discovers the skill.

### The inventory

- From the tool's first operation in a project, `ai/SKILLS-IN-USE.md` holds a section the tool writes from the lock, "Skills from outside this project", with three tables: adopted from a source, relied on but not in the project, and retired. It lies between its own `trace-skills` markers, outside the kit's skill-routing block, so a kit update never meets it as a local edit.
- The instruction sections that name the tool changed version: `core-workflow` in `AGENTS.md` is now version 3 and `skill-routing` in `ai/SKILLS-IN-USE.md` version 2. An unedited section updates as before. A project that edited one and declared it compatible in `ai/project.json` declares it again for the new version.

### The library

- The kit's authors keep a TRACE-compatible skill library at <https://github.com/amrhs-zohrevand/trace-skill-library>, private for now. It holds 34 packages, listed in its README, and six more are held back until their rights are settled.
- All four worked examples still ship in the kit (`meeting-and-idea-intake`, `literature-processing`, `zotero-knowledge-base`, `claim-evidence-audit`), and no removal is scheduled.

### Setup and update

- The starter's `.gitignore` and `.gitattributes` do not change. The tool's operation journal lives at `.trace-kit/receipts/skill-operation.json`, in a folder the kit's ignore rules already cover. A package holding a file Git would rewrite on commit, such as a text file with CRLF line endings, is refused before anything is written, because its recorded digest would not survive a clone.
- The tool's three test modules are part of the release, so an installed project's workflow check runs them with the kit's others.

### Example methods

- The `compatibility` field of `zotero-knowledge-base` was one character over the Agent Skills specification's limit of 500, so definition 1 reported the kit's own example as format invalid; it is shorter now and states the same requirements (AH-232).

### Limits

- An update's `--rollback` refuses when a file the update wrote changed afterwards, and a skills operation after a kit update changes such files: `.trace-kit/install.json` whenever it writes agent copies, and `ai/SKILLS-IN-USE.md` after an update that rewrote its routing block. The update to this release rewrites that block in a project that left it as the kit installed it, so any skills operation after it makes its rollback refuse. Roll back first, then adopt.
- A later release that starts shipping a name a project adopted is an update conflict the person resolves; the tool offers no step for it.
- Later-run discovery sees the `SKILL.md` digest only, so records made before and after an update that changed only a package's scripts look the same to it.
- `scripts/kit_projects.py` and `bootstrap.py --plan` do not report an unfinished skills operation; `trace_skills.py status` does.
- `status` names a skill of the same name in a personal skill folder and says what the skills pilot observed on 2026-10-01: Claude Code 2.1.280 loaded the personal copy instead of the project's, and Codex 0.154.0 listed personal skills only; other clients and later versions were not rehearsed, and an agent that searches the tree may list the stored originals under `.trace-kit/skill-bases/` as skills, which the README there says they are not.
- The three test modules of the tool ship with it and run in an installed project's workflow check and in an update's release preflight; they add about two minutes to each on the machine this was checked on.
- The updater's release preflight keeps no log when it refuses an update, so a failing release test cannot be named afterwards (AH-250, open). In this candidate's preview one preflight failed once under eight scenarios running at the same time and could not be reproduced alone or under six parallel runs; if an update is refused that way on a loaded machine, run it again.
- The checks ran on macOS with Python 3.13 only; the Windows paths are simulated in tests, as in rc17.

## Changes in 0.1.0-rc17

This candidate fixes what a review of the whole kit found on 2026-09-30: five reviewers, each finding reproduced by an independent checker and read again by Codex, every fix with a test that fails without it, and the fixes rechecked by Codex. Beside the fixes it removes the kit's GitHub workflow and adds one routine, for updating every TRACE project on a machine.

### Records

- `trace_record.py extract` no longer records the harness's own rows as the person's turns: Claude Code's notices (rows it marks `isMeta`, such as "Continue from where you left off.") and Codex's machine rows (subagent notifications, aborted turns, heartbeats, edit notices, the instructions header in either form). It keeps messages queued while an agent worked in every shape Claude Code has written them, and a person who quotes a harness tag keeps every word. Records extracted before this may hold such turns and are worth reading again.
- `<synthetic>` is no longer recorded as an execution agent, and `draft-message` no longer writes it as a trailer.
- The execution trailer is read from the end of a commit message, in any case, by one reader that backfill, `verify-range`, `accept`, the governing-actions report and the agentic reviewer share: a line of a commit's explanation that starts `Execution-agent:` is no longer taken for it, and `Execution-agent: none` declares no machine.
- A `Record:` line that names a record by its bare id or by an absolute path joins the same record in `queue`, `brief`, `accept --record`, `verify-range` and the report.
- A record begun by `comments` is checked under the current schema; `backfill` with a mistyped range is an error and leaves its file as it was; `verify` reports a malformed coverage entry instead of crashing.

### Acceptance and registries

- A person named with `--person` who executed a covered commit, by its trailers, is recorded as self-review, never as acceptance.
- Which decision on a commit stands follows the decisions' times, not their order in the file, so two partners who decide at once get the same answer whoever merges; decisions in the same second resolve refuse over return over accept, and `review-status` says so when it happens.
- What a model or an undeclared actor states, a return, objections, criteria, evidence, is kept in its row as advisory and said to be no decision, where it was dropped without a word.
- `brief` labels a record from the commits still waiting, as `queue` does, and says how many are accepted and what `accept --record` covers.
- `intake` keeps an explicit `--path` as given, with its digest when the file is readable, and says when it is missing; an empty `--what` is written as `(unnamed)`; a record is cited only by its whole id; a refused default adoption is not asked for a later run.
- `review-status`, `queue` and `accept` stop with an error in a clone that lacks the review baseline, where they reported nothing waiting.

### The defect ledger and review

- A recheck verifies only when it states its criterion, method, evidence, limits and checker, and, for a corrected default, a named later run under a named instruction version; a thinner recheck is still recorded, as thin. After a failed recheck, `mark --fixed` records the new correction and its fixer, so the next recheck checks the right fix. A second failed recheck of a verified correction stays `recurred`.
- `check` exits 2 when the ledger file is missing and notes a verified row whose faulty default no later run covers; `mark` names an id it did not find.
- Quote grounding checks every fragment of a quote, however short; it reads issue headings and quote lines written in Markdown, bold, bullets or capitals, counts the headings it could not parse, and strips `%` comments only from `.tex` sources.
- The agentic reviewer records the reviewer's own disposition in a delegated row, never an override, and stays advisory without `--authority`. On a timeout it stops every process the reviewer started. It reads quote evidence as UTF-8 whatever the locale and labels a file that is not UTF-8 `not compared`. A `--path` naming a trace record or a registry needs `--inspected-by`. A check run twice is labelled by its latest run, a quote read through a file tool counts as seen, and the packet is removed however the run ends.

### Setup and update

- An update interrupted during its closing check, by Ctrl-C, a timeout or SIGTERM, restores the previous installation, stops the check's processes and records the attempt. `--plan`, `--apply`, `--update-chain`, `--rollback` and `--recover` find an update left unfinished, and `--rollback` reports one it restored apart from the rollback itself.
- A file you edited that the new release leaves unchanged, such as `.gitignore` with lines of your own, no longer stops an update: it is kept and listed.
- An edit saved while an update runs is refused rather than overwritten. Re-running `--apply` after an update keeps the archive's digest, so rollback still works. A deleted instruction file is named as a conflict with how to restore it. Shell scripts keep their executable mode through rollback and recovery.
- An untouched install from 0.1.0-rc2 updates through the whole chain; it stopped at the step to rc5.
- An example skill you remove is retired: `--sync-skills` removes its unchanged copy and records it, apply and update leave it out, and `SETUP-AGENT.md` says how to take it back. Operating-system files such as `.DS_Store` are no longer taken for skill files.
- The release archive has the same bytes on whichever platform it is built.
- `scripts/kit_projects.py` updates every TRACE project on a machine. `list --root DIR` shows each project under the folders you name with its release, profile, agent and whether it has uncommitted changes. `plan` checks the release archive's digest once and runs that release's own updater in plan mode on each project, changing nothing. `update --all`, or `--project` for chosen ones, shows the same plan, applies only after a yes at the terminal or `--yes`, skips a project with uncommitted changes, takes an older install through the chain with `--release-store`, stops at the first conflict or failure unless `--keep-going`, and never commits or pushes. `--download` fetches the release with `gh` when it is installed; nothing requires it. `SETUP-AGENT.md` says how, and an agent asked to update all your TRACE projects is sent there.

### Checks

- In an installed project the workflow check runs the kit's own test modules, those `.trace-kit/install.json` lists, and names the project's other test files as not run, where a project test needing a package the isolated run lacks failed the check. A project's own single-file skill with a shipped skill's name no longer fails the handoff test.
- The suite's result is read from its own final summary; one undecodable byte no longer loses a run; a crash of the build checker is an execution error with its message kept; a bibliography-style comparison the checker did not make reads not checked. `TRACE-STATS.md` shows what was skipped or not run.
- `review_inbox.py` finds `REVIEW` markers in every tracked file, `.github/`, dotfiles and names with non-ASCII characters included, reads them as UTF-8, and `check --json` exits as `check` does.
- The kit no longer ships a GitHub workflow, on the lead researcher's direction: the checks run on the researcher's own machine, and `python scripts/check_workflow.py --out workflow-check.json` is the one command. An update removes an unchanged copy of `.github/workflows/workflow-check.yml`, and of `scripts/ci_commit_range.sh` and its test, which served only that workflow; an edited copy is named as a conflict. `trace_record.py verify-range` stays, run by hand.

### Example methods

- Zotero: BibTeX entries and values are read with their braces and escapes counted, so an entry on one line no longer gives its DOI to the next; a Crossref DOI is taken only for a whole-title match; clean text is written, attached and indexed only for text judged good or partial; enrichment checks each write and never posts a second card; a failed cross-cut or batch is reported; `delete-old` keeps the cross-cut collection and every ancestor of a kept one and says that it deletes outright, subcollections included; `(CITE [search online])` searches with its sentence; every write stays behind `--write`, now tested by behaviour.
- `meeting_quotes.py` credits each labelled speaker in a `.vtt` cue with their own words and counts lines as an editor does.
- The installed skills and `SETUP-AGENT.md` were read together and corrected where they disagreed with the code or each other: `--write` before the subcommand, what the reorganisation plan file is and is not, the DOI author rule, when an absent kit test fails the check, the Docling extractor beside Poppler, and a `check_workflow.py` command missing its `--out`.

### Release

- The supported pair 0.1.0-rc16 to 0.1.0-rc17 is nominated, with unchanged profiles, agent adapters and schemas, and the rc16 archive joins the pinned `releases` list with the digest its release page publishes.
- Limits: each of the 105 defects this candidate fixes in the kit was rechecked by Codex, gpt-6-astra as requested, a model family other than the fixers', which reproduced the original fault on the code before the fix and found it corrected after; each is recorded as verified, and a model's recheck is advisory, not a person's. Three failed their first recheck and were fixed again before they passed. One more, found when the release's own tests ran from a background job, where SIGINT starts ignored, is fixed in a test and checked by running the final archive's update scenarios that way (AH-228). A project that keeps its own single-file skills beside the kit's fails the installed skill parity check until they are converted to the kit's `.skills/<id>/SKILL.md` form, as was done for the two projects where it was found (AH-205). Code paths that run only on Windows were simulated, and nothing in this candidate's checks ran on Windows or Linux. Native skill discovery in an interactive agent session and another researcher's setup trial were not part of this candidate's checks.

## Earlier releases

The changes in 0.1.0-rc1 to 0.1.0-rc15 are on each candidate's release page at https://github.com/amrhs-zohrevand/traceware/releases (the repository was `trace-kit-starter` then), and in the RELEASE-NOTICE.md each candidate's archive shipped.
