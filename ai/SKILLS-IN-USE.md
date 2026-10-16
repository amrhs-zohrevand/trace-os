<!-- trace-os:begin skill-routing -->
# Project-local skills

The canonical skills are under .skills/. Bootstrap installs checked copies for the selected agent; edit the canonical files, then run scripts/bootstrap.py --sync-skills in the initialized project unless it uses --agent none.

Two kinds of skill ship, and the names say which is which.
A `trace-` prefix marks a governance entry point over the five functions, so an installed copy stays distinct from the other skills in an agent's skill directory.
Since 0.1.0-rc15 each of those ids also echoes its TRACE letter: trace-state for T, trace-review for R, trace-acceptance for A, trace-ctransition for C and trace-evaluate for E, with trace-check across all five.
A research method keeps a descriptive name, because TRACE governs how its work is recorded, not whether the method is valid.

## Governance entry points

| Skill id | Display name | Use it when | Status |
| --- | --- | --- | --- |
| trace-state | Record human direction and machine execution | A session's human input, choices, checks and provenance need a record | Draft |
| trace-review | Review against sources | A defined candidate needs grounded advisory findings against named sources; a commit or a path goes to the kit's agentic reviewer on whichever of the two headless CLIs the project has, and the run writes the check row | Draft |
| trace-acceptance | Record an independent acceptance decision | An authorized actor accepts, returns or refuses a defined candidate | Draft |
| trace-ctransition | Check and record a transition | Outside material enters, a draft is promoted, or a standing default is adopted | Draft |
| trace-check | Check the workflow mechanically | The project's mechanical checks should run and their report be read | Draft |
| trace-evaluate | Take findings into the work and recheck that the correction worked | A supported finding needs a ledger row before it is fixed, or a correction needs a targeted recheck on each object it reaches | Draft |

Start at trace-state; it routes to the other five entry points.

## Worked examples: research methods under TRACE

Each example is an ordinary research method whose run hands its work to the record, and each shows a part of TRACE at work.
Read in order, they follow one piece of research from a meeting to a checked claim.

| Skill id | Research step | What it shows about TRACE | Needs | Status |
| --- | --- | --- | --- | --- |
| meeting-and-idea-intake | A meeting or a noted idea directs the work | T: the words that directed the work are kept verbatim, with the speakers' consent, and the speakers become its owners; `scripts/meeting_quotes.py` finds each quote in the transcript | A transcript as `.txt` or `.vtt` | Draft |
| literature-processing | One source is read into a note and a coding record | C: outside material enters with its digest and an intake row, and an extractor that is not there is reported, not assumed | Optional text extractors | Draft |
| zotero-knowledge-base | The shared reference library is kept in order | C: every change to the library is planned before it writes, and writes only with `--write` | A Zotero Web API key in `.env`; numpy and a provider for the semantic passes | Draft |
| claim-evidence-audit | A draft's claims are checked against the evidence | R and A kept apart: support a review finds is not acceptance | Nothing | Draft |

Each was adapted from the kit authors' own research workflow and reviewed and tested before it shipped, and its entry into the kit is recorded in the kit's own intake registry.
They are examples, not endorsements: replace or extend them with your own; the handoff step is the part to keep.
To remove one, retire it as `SETUP-AGENT.md` describes (delete its folder under `.skills/` and run `python3 scripts/bootstrap.py --sync-skills`); to bring it back, remove its id from `retired_skills` in `.trace-os/install.json` and apply the release again.

## Adding a method

1. Write `.skills/<id>/SKILL.md` under a descriptive name, ending with the handoff step from `ai/SKILL-RUN-CONTRACT.md`: one check row with `procedure` set to `<id>`.
2. Add a row for it to the table above.
3. Unless the project uses `--agent none`, run `python3 scripts/bootstrap.py --sync-skills` so the selected agent's copy is installed.
4. If the text came from outside this project, record it: `python3 scripts/trace_record.py intake --what ... --origin ... --by ... --person --checked ... --used-for ... --path .skills/<id>/SKILL.md`, with `--agent` or `--authorized-by` in place of `--person` when that is who decided.
5. A skill is a standing instruction. Record its adoption against the commit that adds it: `python3 scripts/trace_record.py transition --kind default-adoption --object .skills/<id>/SKILL.md --commits <sha>`.

A skill taken from another Git repository comes in through `python3 scripts/trace_skills.py`, as the section Skills from a source in `SETUP-AGENT.md` describes: it copies the package and keeps its untouched original, records the intake, and after the commit its `link` command writes the transition row.
The tool lists those skills in its own section of this file, "Skills from outside this project", which it writes itself and which is not edited by hand.

`python3 scripts/check_skill_handoff.py` reports whether each skill states its handoff; `trace_report.py` reports whether the adoption row exists. Both report; neither blocks.

The shared handoff fields are defined in `ai/SKILL-RUN-CONTRACT.md`.
No user-global writing skills are required. Agent-specific discovery and user-global overrides must be checked in each environment.
<!-- trace-os:end skill-routing -->
