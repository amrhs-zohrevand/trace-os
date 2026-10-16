<!-- trace-os:begin working-routine -->
# Working routine

Start with a bounded question and name the input objects, intended output, and check that would establish completion.
Read AGENTS.md and the relevant project-local skill. Keep the agent's proposed changes reviewable in Git.

1. Record new sources and data in the intake record, with the checks performed before use.
2. Draft and analyze against named versions of the inputs. Bulky files live in the shared folder; an analysis whose results will be reported reads a verified local snapshot of them.
3. Review claims against their sources and keep a record of the criterion, method, result, checker and limits.
4. Record defects and targeted rechecks. A model review is not human acceptance.
5. Run the mechanical checks, render project health, complete the trace record and update the workflow log.

Record the human direction and each consequential choice, with its reason, as the session goes rather than at its end; step 5 completes the trace record.

The core runs without model credentials or cloud access. Team storage is the shared folder the person's own sync app keeps, or a bucket the agent copies with rclone (SETUP-AGENT.md, Shared folder); Git records what it should hold in sources/shared-inventory.json, and the kit itself shares nothing.
Git history, shared-folder access, manuscript editing and publication each have their own scope. Keep those scopes explicit when adding a service.
<!-- trace-os:end working-routine -->
