"""agent-memory launcher: `launcher.py hook <Event>` (Claude Code hooks) or `launcher.py cli <command>`.

Kept free of newer syntax so that an old python reaches the version check and exits with code 3
instead of a syntax error (the hook wrapper then tries the next python or prints a hint).
"""
import os
import sys

if sys.version_info < (3, 10):
    sys.stderr.write("agent-memory needs Python 3.10 or newer; this is %d.%d (%s)\n"
                     % (sys.version_info[0], sys.version_info[1], sys.executable))
    sys.exit(3)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "hook":
        from agent_memory import hook
        return hook.main(sys.argv[2:])
    if mode == "cli":
        args = sys.argv[2:]
        if args[:1] == ["doctor"]:
            from agent_memory.plugin import doctor_main
            return doctor_main(args[1:])
        if args[:1] == ["selftest"]:
            from agent_memory.plugin import selftest_main
            return selftest_main(args[1:])
        from agent_memory.__main__ import main as cli
        return cli(args)
    sys.stderr.write("usage: launcher.py hook <SessionStart|UserPromptSubmit> | launcher.py cli <command>\n")
    return 2


sys.exit(main())
