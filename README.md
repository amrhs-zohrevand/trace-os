# TRACE-OS

TRACE-OS is an operating system for AI-assisted research projects: the shared environment in which researchers direct the work and AI agents, such as Claude Code or Codex, carry it out and record it.
It puts the five TRACE principles into practice: Track records, Review against sources, Accept independently, Control transitions, and Evaluate corrections.
It consists of shared instructions, command-line tools and plain record files kept with the project, and works alongside the skills and workflows a research team already uses.
For each piece of work, it keeps records of what was asked of an AI agent, what the agent chose, what was checked, and who accepted it.
The core tools use Git and Python and need no server, database or particular model subscription.
The principles and TRACE-OS are introduced in *TRACE: Governing AI-Assisted Research with Principles from Computer Science* (2026).
TRACE-OS is distributed under a perpetual, non-exclusive licence, as its paper is on arXiv, with all other rights reserved; see LICENSE.
The full compatibility matrix is pending; see RELEASE-NOTICE.md.
TRACE-OS was called TRACEware in 0.1.0-rc19 and the TRACE kit (`trace-kit`) up to 0.1.0-rc18.

Give an existing agent this instruction:

> Read SETUP-AGENT.md. Set up the academic profile in my chosen directory, run the checks, and report any remaining configuration.

The same setup works without an agent. Python 3.11 or later and Git are required. LaTeX is optional until you build the paper or slides. No model subscription or cloud account is needed for core checks.

```bash
python scripts/bootstrap.py --target ../my-research --plan
python scripts/bootstrap.py --target ../my-research --apply
```

A new project gets fresh Git history and empty research registries. Existing Git projects retain their history; setup reports conflicting files before writing.
Read SETUP-AGENT.md for in-place setup, recovery, upgrades, skill installation and the setup receipt.
An update follows a release pair the new release's manifest lists; SETUP-AGENT.md says how, and RELEASE-NOTICE.md which release this is. It reads historical trace records as version 1 and writes new records as version 2.
On Windows, use `py -3.11` in place of `python` if the Python launcher selects the required version. On macOS, use `python3` when no `python` command is installed.
