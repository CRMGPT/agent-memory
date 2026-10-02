# Changelog

## 0.1.1 - 2026-10-02

- Fix: `end --diff` and `commit` crashed with a traceback on a JSON list of records, which is the
  form the session-start instruction described; a single record was silently not saved by `end`.
  Both forms are now accepted (each record becomes an ADD), the full `{"ops": [...]}` form works
  as before, and any other shape is refused with exit code 2 before anything is written, by both
  `end` (the episode stays open) and `commit`.
- Unchanged: evidence written as a `kind:ref` string is still accepted, and an empty diff with
  `--summary` still closes the episode with its summary.
- An unreadable diff file (a directory, not UTF-8) is a clean refusal instead of a traceback.
- The hook instruction, the skill, the executor role and the README show the exact accepted file.

## 0.1.0 - 2026-10-02

First public version, packaged as a Claude Code plugin.

- Per-project memory: facts, decisions, constraints and bugs with sources, in four sections
  (code, logic, data, workflow); worktrees of one repository share it, other projects never see it.
- Hooks (SessionStart, UserPromptSubmit) add a short read-only context and a trusted block with the
  exact command for this machine; nothing is written by the hooks.
- One writer per task: begin / end with a knowledge diff / session finish; the lease owner is the
  Claude Code process; an unknown owner can be taken over only with a human's confirmation.
- Facts are shown with their current certainty and sources in this worktree; withdrawn facts that
  were already shown are announced once.
- `doctor` and `selftest` commands; the hook needs Python 3.10+ and only says what to install when
  it is missing.
