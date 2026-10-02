"""Plugin mode: where the plugin lives, which MEM command works on this machine, doctor and selftest.

The plugin is installed by Claude Code (`/plugin install`); nothing here edits Claude settings.
* PLUGIN_ROOT — the directory with `.claude-plugin/plugin.json`; it changes with every plugin
  update, so no state is kept there.
* data_dir() — per-user plugin data (hook cache, last hook calls): `$CLAUDE_PLUGIN_DATA` when
  Claude Code runs the hook, otherwise `<user data>/agent-memory/plugin`
  (`data_home()` already ends with `agent-memory`).
* mem_commands() — the exact command that runs THIS copy of the CLI for each shell of this
  machine, quoted for that shell (Git Bash and PowerShell on Windows, sh elsewhere). The hook
  prints it in the trusted `<memory-integration>` block, so agents never guess paths.
* doctor() — read-only checks (python, plugin files, data dir, project resolution, that each
  command really runs this copy); selftest() — test suite + doctor into a diagnostics file with
  personal paths redacted and without any memory contents.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PLUGIN_ROOT / "bin" / "launcher.py"
MIN_PYTHON = (3, 10)
WSL_PLACEHOLDER = "<WSL_PROJECT>"
_PS_SIMPLE = re.compile(r"[A-Za-z0-9_./\\:-]+")


def meta() -> dict:
    try:
        return json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def version() -> str:
    return str(meta().get("version") or "dev")


def data_dir() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    from .paths import data_home

    return data_home() / "plugin"


# ------------------------------------------------------------------ commands per shell


def quote_for(shell: str, text: str) -> str:
    """One word for the target shell: sh/Git Bash - single quotes (shlex); PowerShell - single
    quotes with every apostrophe kind doubled. Inside single quotes $, ` and " are not expanded."""
    if shell == "powershell":
        if _PS_SIMPLE.fullmatch(text):
            return text
        for ch in "'‘’‚‛":
            text = text.replace(ch, ch * 2)
        return "'" + text + "'"
    return shlex.quote(text)


def fill_wsl_project(cmd: str, key: str, path: str) -> str | None:
    """Put the project's path inside WSL into a command, quoted for that command's shell. None - the
    path cannot be passed safely from this shell (Windows PowerShell 5.1 mangles an argument with a
    double quote when it starts a program): use the Git Bash command."""
    shell = "powershell" if key.endswith("powershell") else "bash"
    if shell == "powershell" and '"' in path:
        return None
    q = quote_for(shell, path)
    return cmd.replace(f'"{WSL_PLACEHOLDER}"', q).replace(WSL_PLACEHOLDER, q)


def mem_commands(python: str | None = None, wsl_distro: str | None = None,
                 wsl_launcher: str | None = None) -> dict[str, str]:
    """{shell key: command} that runs this plugin copy's CLI. With wsl_distro/wsl_launcher - the
    commands for a project inside WSL opened from native Windows (<WSL_PROJECT> to be filled)."""
    python = python or sys.executable
    if wsl_distro and wsl_launcher:
        d, lw = wsl_distro, wsl_launcher
        return {"wsl_from_git_bash": f"MSYS_NO_PATHCONV=1 wsl.exe -d {quote_for('bash', d)} --cd {WSL_PLACEHOLDER} "
                                     f"--exec python3 {quote_for('bash', lw)} cli",
                "wsl_from_powershell": f"wsl.exe -d {quote_for('powershell', d)} --cd {WSL_PLACEHOLDER} --exec "
                                       f"python3 {quote_for('powershell', lw)} cli"}
    if sys.platform == "win32":
        def fwd(p):
            return str(p).replace("\\", "/")

        return {"git_bash": f"{quote_for('bash', fwd(python))} {quote_for('bash', fwd(LAUNCHER))} cli",
                "powershell": f"& {quote_for('powershell', str(python))} {quote_for('powershell', str(LAUNCHER))} cli"}
    return {"shell": f"{shlex.quote(python)} {shlex.quote(str(LAUNCHER))} cli"}


LABELS = {"git_bash": "Git Bash", "powershell": "PowerShell", "shell": "shell",
          "wsl_from_git_bash": "Git Bash", "wsl_from_powershell": "PowerShell"}


# ------------------------------------------------------------------ Windows -> WSL


def wsl_launcher(distro: str, timeout: float = 6.0) -> str | None:
    """This plugin copy's launcher as a path inside the given WSL distribution (`wslpath -u`), cached
    per (distro, plugin root): the root changes with every plugin update. `timeout` - the caller's
    share of the hook time limit (the first call may have to wake WSL up)."""
    cache = data_dir() / "wsl-paths.json"
    key = f"{distro.lower()}|{PLUGIN_ROOT}"
    try:
        known = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if isinstance(known, dict) and isinstance(known.get(key), str):
        return known[key]
    try:
        out = subprocess.run(["wsl.exe", "-d", distro, "--cd", "/", "--exec", "wslpath", "-u", str(LAUNCHER)],
                             capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = out.stdout.decode("utf-8", errors="replace").strip()
    if out.returncode != 0 or not path.startswith("/") or not path.endswith("/bin/launcher.py"):
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        known = known if isinstance(known, dict) else {}
        known[key] = path
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(known), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        pass
    return path


# ------------------------------------------------------------------ doctor


def _run_where(argv: list[str], cwd: Path) -> tuple[bool, str, dict]:
    try:
        out = subprocess.run(argv, capture_output=True, timeout=90, stdin=subprocess.DEVNULL, cwd=str(cwd))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}", {}
    text = out.stdout.decode("utf-8", errors="replace")
    try:
        data = json.loads(text[text.index("{"):]) if "{" in text else {}
    except ValueError:
        data = {}
    detail = f"exit {out.returncode}" + ("" if out.returncode == 0 else ": " + out.stderr.decode(
        "utf-8", errors="replace").strip()[-200:])
    return out.returncode == 0 and isinstance(data, dict) and "side" in data, detail, data


PROBE_DIR = "probe dir $HOME 'q' `b`"  # space, $, quote and backtick: proves the quoting of the commands


def doctor(cwd: Path | None = None) -> dict:
    """Read-only checks. Creates and removes only its own temporary directory."""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    check("python 3.10 or newer", sys.version_info >= MIN_PYTHON, f"{platform.python_version()} at {sys.executable}")
    m = meta()
    check("plugin manifest", m.get("name") == "agent-memory", f"{m.get('name')} {m.get('version')}")
    for rel in ("hooks/hooks.json", "hooks/run-hook.sh", "bin/launcher.py", "skills/project-memory/SKILL.md",
                "agents/executor.md", "agents/researcher.md", "agents/reviewer.md"):
        check(f"plugin file {rel}", (PLUGIN_ROOT / rel).is_file())
    d = data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".doctor-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("plugin data directory writable", True, str(d))
    except OSError as exc:
        check("plugin data directory writable", False, f"{d}: {exc}")
    if cwd is not None:
        from .paths import resolve_project

        proj = resolve_project(Path(cwd), create=False)
        check("project for the current directory", True,
              "memory disabled here (.agent-memory-off)" if proj.disabled else
              (f"store {proj.store}" if proj.store else f"no project memory here ({proj.reason or proj.kind})"))
    root = Path(tempfile.mkdtemp(prefix="am-doctor-"))
    tmp = root / PROBE_DIR
    tmp.mkdir()
    want = str(PLUGIN_ROOT / "agent_memory").replace("\\", "/").lower()
    try:
        for key, cmd in mem_commands().items():
            if key == "git_bash":
                bash = shutil.which("bash")
                if not bash or "system32" in bash.lower():
                    check(f"command ({key}) runs this plugin copy", False, "Git Bash not found on PATH")
                    continue
                argv = [bash, "-c", f"cd {quote_for('bash', tmp.as_posix())} && {cmd} where"]
            elif key == "powershell":
                ps = shutil.which("powershell") or "powershell.exe"
                argv = [ps, "-NoProfile", "-NonInteractive", "-Command",
                        f"Set-Location -LiteralPath {quote_for('powershell', str(tmp))}; {cmd} where"]
            else:
                argv = ["sh", "-c", f"cd {shlex.quote(str(tmp))} && {cmd} where"]
            ok, detail, data = _run_where(argv, root)
            code = str(data.get("code_path", "")).replace("\\", "/").lower()
            check(f"command ({key}) runs this plugin copy", ok and code == want, f"{detail}; code {code or '?'}")
    finally:
        shutil.rmtree(root, ignore_errors=True)  # only the directory created by this call
    if sys.platform == "win32":
        check("WSL available (projects inside WSL)", shutil.which("wsl.exe") is not None,
              "optional: needed only for projects that live inside WSL")
    try:
        last = json.loads((d / "last_hook.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    required = [c for c in checks if not c["check"].startswith("WSL available")]
    return {"ok": all(c["ok"] for c in required), "version": version(), "plugin_root": str(PLUGIN_ROOT),
            "python": sys.executable, "commands": mem_commands(), "checks": checks, "last_hook_calls": last}


# ------------------------------------------------------------------ selftest


def _redactor():
    pairs = []
    for value, label in ((str(PLUGIN_ROOT), "<plugin>"), (tempfile.gettempdir(), "<tmp>"),
                         (os.path.expanduser("~"), "~")):
        if value and len(value) > 3:
            for v in {value, value.replace("\\", "/"), value.replace("\\", "/").lower(), value.lower()}:
                pairs.append((v, label))
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    host = platform.node()
    names = [(word, label) for word, label in ((user, "<user>"), (host, "<host>")) if word and len(word) > 1]

    def redact(text: str) -> str:
        for v, label in sorted(pairs, key=lambda x: -len(x[0])):
            text = text.replace(v, label)
        for word, label in names:
            text = redact_name(text, word, label)
        return text
    return redact


def redact_name(text: str, word: str, label: str) -> str:
    """Replace a user or host name only where it is a WHOLE path component (`/x/NAME/y`, `C:\\x\\NAME`)
    or a side of `NAME@host` / `user@NAME`. Inside a longer word ("test" in "tests", "latest",
    "test_x") and as an ordinary word of a sentence ("1 test passed") it stays as it is."""
    rx = re.compile(rf"(?<![\w.-]){re.escape(word)}(?![\w.-])", re.IGNORECASE)

    def sub(m: re.Match) -> str:
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        return label if before in ("/", "\\", "@") or after in ("/", "\\", "@") else m.group(0)
    return rx.sub(sub, text)


def selftest(out_path: Path, run_tests: bool = True) -> int:
    """Test suite of this plugin copy + doctor -> a diagnostics file to send back. No prompts, no
    memory contents; home, user, host, temp and plugin paths are replaced by placeholders."""
    redact = _redactor()
    lines = [f"agent-memory selftest {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
             f"plugin version: {version()}", f"os: {platform.platform()}", f"machine: {platform.machine()}",
             f"python: {platform.python_version()} ({sys.executable})"]
    started = time.monotonic()
    try:
        import pytest  # noqa: F401
        has_pytest = True
    except ImportError:
        has_pytest = False
    if not run_tests:
        lines.append("tests: skipped by the caller")
        rc_tests = None
    elif not has_pytest:
        lines += ["tests: NOT RUN - pytest is not installed for this python.",
                  "  install it into a temporary environment and run selftest from there:",
                  "  python3 -m venv /tmp/am-venv && /tmp/am-venv/bin/python -m pip install pytest",
                  "  then: /tmp/am-venv/bin/python <plugin>/bin/launcher.py cli selftest"]
        rc_tests = None
    else:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AGENT_MEMORY_", "CLAUDE_PLUGIN_"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rfEs", "-p", "no:cacheprovider", "tests"],
                             cwd=str(PLUGIN_ROOT), capture_output=True, env=env, timeout=3600,
                             stdin=subprocess.DEVNULL)
        rc_tests = out.returncode
        text = out.stdout.decode("utf-8", errors="replace").splitlines()
        keep = [ln for ln in text if ln.startswith(("FAILED", "ERROR", "SKIPPED", "E   ")) or " passed" in ln
                or " failed" in ln or " error" in ln][-80:]
        lines += [f"tests: exit {rc_tests} in {time.monotonic() - started:.0f} s", *("  " + ln for ln in keep)]
    doc = doctor(Path.cwd())
    lines.append(f"doctor: ok={doc['ok']}")
    lines += [f"  - {c['check']}: {'ok' if c['ok'] else 'FAIL'} {c['detail'][:200]}" for c in doc["checks"]]
    lines.append("last hook calls: " + json.dumps(doc["last_hook_calls"]))
    text = redact("\n".join(lines) + "\n")
    out_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"diagnostics written to {out_path}")
    return 0 if doc["ok"] and rc_tests in (0, None) else 1


def _utf8_out() -> None:
    from .__main__ import _utf8_pipes

    _utf8_pipes()


def doctor_main(argv: list[str]) -> int:
    _utf8_out()
    report = doctor(Path.cwd())
    sys.stdout.write(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    return 0 if report["ok"] else 1


def selftest_main(argv: list[str]) -> int:
    _utf8_out()
    out = Path(argv[0]) if argv else Path.cwd() / "agent-memory-diagnostics.txt"
    return selftest(out)
