# agent-memory

Persistent memory for each of your projects in Claude Code.

Claude forgets everything between sessions. agent-memory keeps what was learned while working on a
project: decisions, rules, facts about the code, data and workflow, each with the file or check it
came from. When you open a new session in that project, Claude gets a short summary of what applies
right now, and more with every prompt when it is relevant. Other projects never see it.

[Русская версия](README.ru.md)

## How it works

- Reading is automatic and read-only. Two hooks run when a session starts and when you send a
  prompt. They add a short block `<project-memory read-only="true">` to Claude's context. They never
  write to project memory: they only keep a small cache of what this session was already shown and a
  diagnostic file `last_hook.json` in the plugin's data folder (`${CLAUDE_PLUGIN_DATA}`). They mark
  the text as data, not instructions.
- Writing is done by one agent per task. For a task that changes or investigates code, the
  agent doing the work (the main session, or the `agent-memory:executor` subagent) starts an
  episode, and after the result is checked saves only new verified knowledge. You do not have to
  say "remember". Plain conversation writes nothing.
- Every record has a source. A record is one claim (a decision, a fact, a constraint, a bug)
  with evidence: a file, a commit, a test, a document. Claude sees how certain it is right now:
  if the file changed or the test no longer passes, the record is shown as needing a recheck.
- Four sections: `code` (structure, modules), `logic` (rules, decisions), `data` (schemas,
  formats, sources), `workflow` (how to run, test, deliver).
- Branches and worktrees are respected. Knowledge recorded on another branch, or on
  uncommitted work in another worktree, is not presented as a fact here. If a fact that Claude
  already saw is withdrawn, the next summary says so once.
- Projects are isolated. Worktrees of one git repository share one memory; separate clones and
  other projects do not.
- One writer at a time. The writer holds a lease tied to its Claude Code process. If that
  process cannot be proven dead, nobody takes over without a human's confirmation.

## Requirements

- Claude Code with plugin support.
- Python 3.10 or newer: [python.org](https://www.python.org/downloads/), Homebrew
  (`brew install python`) or the Windows `py` launcher. The `python3` that comes with the macOS
  Command Line Tools is 3.9 and is too old. Without a suitable Python the plugin stays off and says
  once, at session start, what to install. It never blocks Claude.
- git.

## Install

In Claude Code:

```
/plugin marketplace add CRMGPT/agent-memory
/plugin install agent-memory@agent-memory
```

From a local folder instead of GitHub: `/plugin marketplace add /path/to/agent-memory`.
Start a new session after installing. To check the installation, run in a terminal (the exact
command is printed in the `<memory-integration>` block at session start; this is the general form):

```
python3 "<plugin folder>/bin/launcher.py" cli doctor
```

## Use

Nothing special is needed: work as usual. At session start Claude gets a trusted block from the
plugin with the exact memory command (`MEM`) for your computer and shell. The executor uses it:

```
MEM begin "<task>"                          # prints the session token
MEM end --session <token> --diff diff.json --summary "<one line>"
MEM session finish --session <token>        # only when the whole task is done
```

Other useful commands: `MEM where` (which memory this folder uses, read-only), `MEM search "<words>"`,
`MEM doctor`, `MEM selftest [file]` (runs the tests and writes a diagnostics file without personal
paths or memory contents). The skill `agent-memory:project-memory` describes the full procedure.

## Where the data lives

| System | Project memory |
| --- | --- |
| Windows | `%LOCALAPPDATA%\agent-memory\projects\<id>\` |
| Linux, WSL | `$XDG_DATA_HOME/agent-memory/projects/<id>/` (default `~/.local/share/...`) |
| macOS | `~/Library/Application Support/agent-memory/projects/<id>/` |

The project id is stored in `.git/agent-memory.json` (inside `.git`, not in your files). A folder
without git becomes a project only when you mark it with `MEM init-project`. The hooks' session
cache (`sessions/`), `last_hook.json` and, on Windows, `wsl-paths.json` live in the plugin's data
folder that Claude Code provides (`${CLAUDE_PLUGIN_DATA}`); there is no project memory in it.

A project inside WSL opened from Claude Code on Windows is read and written inside WSL by the same
plugin copy (it needs Python 3.10+ inside WSL).

## Turn it off

- One project: create an empty file `.agent-memory-off` in the project root, or set
  `"agent-memory@agent-memory": false` under `enabledPlugins` in `.claude/settings.local.json`.
- Everywhere: `/plugin disable agent-memory@agent-memory`, or remove it with
  `/plugin uninstall agent-memory@agent-memory`. Uninstalling keeps the project memory; delete the
  folders from the table above if you want it gone.

## If the owner is unknown

If a writing session ended abnormally, the next writer may be refused with owner state `unknown`.
This is on purpose: without proof that the previous process is gone, two writers could mix their
work. Check that the previous session is really finished, then confirm it yourself:

```
MEM session recover --operator-confirmed "<what you checked>"
```

An agent never adds this flag on its own.

## Privacy

Do not put secrets, `.env` values, client records or chat transcripts into memory. Text in known
secret formats (API keys, tokens, private keys, `password=...`) is refused on every write path and
nothing is saved, but this is a filter of known formats, not a guarantee: an unusual secret may
pass. Memory stays on your computer; the plugin sends nothing anywhere.

## Limitations

Verified: Windows (native and with WSL). Not verified: macOS, Linux outside WSL — support is not confirmed.

- No guarantees: this is early software (version 0.1.0).
- Memory is as good as what was recorded; check the cited source before relying on a fact.
- The hooks add a bounded amount of text (at most about 6000 characters per event).

## License

MIT, see [LICENSE](LICENSE). Security issues: see [SECURITY.md](SECURITY.md).
