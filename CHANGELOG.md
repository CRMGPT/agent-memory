# Changelog

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
