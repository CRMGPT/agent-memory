#!/bin/sh
# agent-memory hook wrapper (POSIX sh; on Windows Claude Code runs it with Git Bash's sh).
# Finds Python 3.10+ and runs the plugin's launcher with the hook payload from stdin.
# Never blocks Claude: always exits 0. Without a suitable python it says once, at session
# start, what to install.
event="$1"
root="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$root" ]; then
    root=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)
fi
launcher="$root/bin/launcher.py"
if [ -t 0 ]; then payload=""; else payload=$(cat); fi  # a terminal is not an event: never wait for input
old=""

try_python() {
    # $@ = python command (one or two words). 0 = done (output printed), 1 = try the next one.
    out=$(printf '%s' "$payload" | "$@" "$launcher" hook "$event" 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ]; then
        printf '%s' "$out"
        return 0
    fi
    if [ "$rc" -eq 3 ]; then
        old="$old $*"
    fi
    return 1
}

stub=""
macos_stub() {
    # macOS without Command Line Tools ships /usr/bin/python3 as a stub that opens an install dialog
    # every time it runs. Skip it (the hook runs on every prompt); xcode-select -p fails exactly then.
    case "$1" in /usr/bin/python3|/usr/bin/python) ;; *) return 1 ;; esac
    [ "$(uname -s 2>/dev/null)" = "Darwin" ] || return 1
    xcode-select -p >/dev/null 2>&1 && return 1
    stub="$1"
    return 0
}

if [ -n "${AGENT_MEMORY_PYTHON:-}" ] && try_python "$AGENT_MEMORY_PYTHON"; then exit 0; fi
for cand in python3 python; do
    path=$(command -v "$cand" 2>/dev/null) || continue
    macos_stub "$path" && continue
    if try_python "$cand"; then exit 0; fi
done
if command -v py >/dev/null 2>&1 && try_python py -3; then exit 0; fi

if [ "$event" = "SessionStart" ]; then
    if [ -n "$old" ]; then
        why="only an older python was found ($old)"
    elif [ -n "$stub" ]; then
        why="$stub is only the macOS stub until Command Line Tools are installed (it was not started)"
    else
        why="no python was found"
    fi
    hint="agent-memory is off: it needs Python 3.10 or newer and $why. Install Python 3.10+ (python.org, Homebrew: brew install python, or the Windows py launcher) and start a new session."
    printf '{"systemMessage": "%s", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "[project memory] not loaded: %s"}}' "$hint" "$hint"
fi
exit 0
