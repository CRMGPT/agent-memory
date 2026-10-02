# Design

A short map of how agent-memory works inside. User-level documentation: [README](../README.md).

## Pieces

| Part | Where | What it does |
| --- | --- | --- |
| Hooks | `hooks/hooks.json`, `hooks/run-hook.sh`, `agent_memory/hook.py` | SessionStart and UserPromptSubmit: find Python 3.10+, read memory read-only, add a bounded context and the trusted block with the MEM command |
| Read path | `agent_memory/reader.py` | Opens the store `mode=ro` + `query_only`, evaluates each record exactly like `Memory.get`, renders only what applies in this worktree |
| Memory | `agent_memory/memory.py`, `store.py` | Records, evidence, links, transitions in SQLite (WAL); secret-format filter on every write |
| Episodes | `agent_memory/context.py`, `diff.py` | `begin` / `note` / `expand` / `end` with a knowledge diff applied in one transaction |
| Worktree view | `agent_memory/view.py` | Where a record applies: git history of this worktree, uncommitted work, branches |
| Checks | `agent_memory/checks.py` | `pytest` and `pattern` checks bound to the code state they ran on |
| Code map | `agent_memory/codegraph.py` | Per-worktree AST index (Python) so records can point at functions; refreshed by `end` |
| Sessions | `agent_memory/session.py` | Write lease per worktree, owned by the Claude Code process; `unknown` owner needs a human |
| Projects | `agent_memory/paths.py` | Which store a folder uses; project id in `.git/agent-memory.json` |
| Plugin | `agent_memory/plugin.py`, `bin/launcher.py` | Plugin root and data folder, MEM command per shell, `doctor`, `selftest` |

## Records

Types: FACT, DECISION, CONSTRAINT, BUG, HYPOTHESIS, OBSERVATION, QUESTION, DOCUMENT, TEST_RESULT and
episode summaries. Each record is one claim with a section (`code`, `logic`, `data`, `workflow`) and
evidence of a kind: `file`, `code`, `test`, `doc`, `commit`, `tool`, `user`, `url`, `check`,
`episode`, `model`. Status changes only through diff operations (UPDATE, SUPERSEDE, INVALIDATE,
RESOLVE, CONFIRM, LINK, UNLINK, ADD), each with an expected version.

Certainty, strongest first: `verified` (a pytest check passed on the same code state as now),
`check_passed`, `needs_recheck` (the code changed since the check), `sourced` (a checkable source
exists), `attested` (a person's word or a tool output), `probable`, `hypothesis`, `unknown`, and
`contradicted` / `superseded` after INVALIDATE / SUPERSEDE. An agent may declare only `sourced`,
`attested`, `probable`, `hypothesis` or `unknown`; the stronger levels come from checks.

## Where a record applies

Every record and every status change remembers the commit, branch, worktree and, for uncommitted
work, a fingerprint of the files involved. In a worktree a record is:
`history` (its commit is in this worktree's history), `here` (made here on uncommitted work that is
still present), `none` (outside git), `withdrawn` (made here, the work was reverted), `pending`
(uncommitted work of another worktree) or `other` (another branch). Only the first three apply. A
status change made on a branch does not close the record on the main line until the branch is
merged.

## The hooks

The hook never writes to memory. It shows a record only if it applies here and is active, with its
current certainty and sources; inapplicable records are counted, not shown. A per-session cache
(in the plugin data folder) prevents repeats; a record whose fingerprint changed is shown again, and
a shown record that stopped applying is announced once as `WITHDRAWN`. Output is limited (about
6000 characters) and escaped, so stored text cannot close the `<project-memory>` frame.

The trusted `<memory-integration>` block comes from the plugin code, not from stored text. It tells
the single executor how to run the cycle and prints the exact MEM command: for sh on macOS and
Linux, for Git Bash and PowerShell on Windows, and for a project inside WSL opened from Windows (the
plugin copy is reached from WSL through `wslpath`, the store is opened only inside WSL).

## Writing

One writer per worktree holds a lease. The owner is the Claude Code process that ran `begin` or
`session start` (found through the parent process chain). The owner is `dead` only on complete
proof; otherwise it is `unknown`, and only a human can confirm a takeover
(`session recover --operator-confirmed "<reason>"`). `session finish` ends the task: it refuses while
an episode is open.

CLI exit codes: 0 success; 2 input error, nothing saved; 3 conflict, nothing saved; 4 lease (another
session or no token), nothing saved; 5 old store schema, run `migrate`; 6 database saved but a
derived file was not written, run `repair`.
