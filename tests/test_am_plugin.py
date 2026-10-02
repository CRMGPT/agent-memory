"""Plugin form: manifests and layout, the hook wrapper (python discovery, soft failure), the MEM command
printed by the hook and a full cycle with it, doctor, selftest diagnostics, Windows -> WSL dispatch.

Everything runs in temporary projects and temporary data directories; no Claude settings are used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory import plugin
from am_helpers import git, write

ROOT = Path(__file__).resolve().parents[1]
NAME_RX = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", path
    out = {}
    for line in lines[1:]:
        if line == "---":
            return out
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    raise AssertionError(f"{path}: frontmatter not closed")


# ================================================================ layout


def test_manifests_and_plugin_layout():
    meta = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert NAME_RX.fullmatch(meta["name"]) and not meta["name"].startswith(("claude-", "anthropic-"))
    assert meta["name"] == "agent-memory" and re.fullmatch(r"\d+\.\d+\.\d+", meta["version"])
    assert meta["license"] == "MIT" and meta["author"]["name"]
    for key in ("homepage", "repository"):
        assert meta[key].startswith("https://")
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert NAME_RX.fullmatch(market["name"]) and market["owner"]["name"]
    entry = [p for p in market["plugins"] if p["name"] == meta["name"]]
    assert len(entry) == 1 and entry[0]["source"] == "./" and entry[0].get("version", meta["version"]) == meta["version"]
    assert (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").count(meta["version"]) >= 1

    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert set(hooks) == {"hooks"} and set(hooks["hooks"]) == {"SessionStart", "UserPromptSubmit"}
    assert hooks["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"
    for event, groups in hooks["hooks"].items():
        assert len(groups) == 1 and len(groups[0]["hooks"]) == 1
        h = groups[0]["hooks"][0]
        assert h["type"] == "command" and 0 < h["timeout"] <= 60
        assert h["command"] == f'sh "${{CLAUDE_PLUGIN_ROOT}}/hooks/run-hook.sh" {event}'
    assert (ROOT / "hooks" / "run-hook.sh").is_file() and (ROOT / "bin" / "launcher.py").is_file()

    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert [p.parent.name for p in skills] == ["project-memory"]
    assert _frontmatter(skills[0])["name"] == "project-memory"
    agents = {p.stem: _frontmatter(p) for p in (ROOT / "agents").glob("*.md")}
    assert set(agents) == {"executor", "researcher", "reviewer"}
    assert all(a["name"] == stem and NAME_RX.fullmatch(stem) for stem, a in agents.items())
    for text in [p.read_text(encoding="utf-8") for p in [*skills, *(ROOT / "agents").glob("*.md")]]:
        assert "{{" not in text  # no installer placeholders left


def test_no_settings_installer_and_no_user_paths_in_plugin_files():
    assert not (ROOT / "agent_memory" / "install.py").exists()
    rx = re.compile(r"/" r"home/[a-z]|[A-Z]:[\\/]" r"Users[\\/][A-Za-z]")
    for rel in ("hooks/hooks.json", "hooks/run-hook.sh", "bin/launcher.py", ".claude-plugin/plugin.json",
                ".claude-plugin/marketplace.json"):
        assert not rx.search((ROOT / rel).read_text(encoding="utf-8")), rel


# ================================================================ launcher and wrapper


def _env(tmp_path: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "AGENT_MEMORY_", "PYTHONPATH", "CLAUDE_PLUGIN_"))}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(XDG_DATA_HOME=str(tmp_path / "data"), LOCALAPPDATA=str(tmp_path / "data"),
               CLAUDE_PLUGIN_ROOT=str(ROOT), CLAUDE_PLUGIN_DATA=str(tmp_path / "plugin data"))
    env.update(extra)
    return env


def test_launcher_exits_3_with_a_hint_on_old_python(tmp_path):
    code = ("import sys; sys.version_info = (3, 9, 6, 'final', 0); sys.argv = ['launcher.py', 'hook', 'SessionStart']; "
            f"exec(compile(open({str(ROOT / 'bin' / 'launcher.py')!r}, encoding='utf-8').read(), 'launcher.py', 'exec'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert out.returncode == 3 and b"needs Python 3.10 or newer; this is 3.9" in out.stderr and out.stdout == b""


def _posix_sh() -> str | None:
    sh = shutil.which("sh")
    if not sh or "system32" in sh.lower():
        return None
    return sh


def _stub_dir(tmp_path: Path, pythons: dict[str, int]) -> Path:
    """A PATH with only `cat` and `dirname` (wrappers) and fake pythons exiting with the given codes."""
    d = tmp_path / "stub bin"
    d.mkdir()
    for tool in ("cat", "dirname"):
        real = shutil.which(tool)
        assert real, tool
        real = real.replace("\\", "/")
        (d / tool).write_text(f'#!/bin/sh\nexec "{real}" "$@"\n', encoding="utf-8")
    for name, rc in pythons.items():
        (d / name).write_text(f"#!/bin/sh\ncat > /dev/null\nexit {rc}\n", encoding="utf-8")
    for p in d.iterdir():
        p.chmod(0o755)
    return d


@pytest.mark.skipif(_posix_sh() is None, reason="needs a POSIX sh (Git Bash on Windows)")
@pytest.mark.parametrize("pythons,why", [({}, "no python was found"),
                                         ({"python3": 3, "python": 3}, "only an older python was found")])
def test_wrapper_without_a_suitable_python_fails_soft(tmp_path, pythons, why):
    stub = _stub_dir(tmp_path, pythons)
    env = _env(tmp_path, PATH=str(stub) if sys.platform != "win32" else
               os.pathsep.join([str(stub), os.path.dirname(_posix_sh())]))
    if sys.platform == "win32":  # Git Bash: keep only its own tools, drop every python and py launcher
        env["PATH"] = str(stub).replace("\\", "/") + ":" + os.path.dirname(_posix_sh()).replace("\\", "/")
    for event in ("SessionStart", "UserPromptSubmit"):
        out = subprocess.run([_posix_sh(), str(ROOT / "hooks" / "run-hook.sh"), event], input=b'{"cwd": "/"}',
                             capture_output=True, env=env, timeout=60)
        assert out.returncode == 0, out.stderr
        if event == "UserPromptSubmit":
            assert out.stdout == b""  # the hint is given once, at session start
            continue
        data = json.loads(out.stdout.decode("utf-8"))
        assert why in data["systemMessage"] and "Python 3.10" in data["systemMessage"]
        assert data["hookSpecificOutput"]["additionalContext"].startswith("[project memory] not loaded")


def _project(path: Path, fn: str = "f") -> Path:
    write(path, "app/m.py", f"def {fn}():\n    return 1\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def _hook(env: dict, event: str, payload: dict, wrapper: bool = False) -> str:
    argv = ([_posix_sh(), str(ROOT / "hooks" / "run-hook.sh"), event] if wrapper else
            [sys.executable, str(ROOT / "bin" / "launcher.py"), "hook", event])
    out = subprocess.run(argv, input=json.dumps(payload).encode("utf-8"), capture_output=True, env=env, timeout=90)
    assert out.returncode == 0, out.stderr[-400:]
    return json.loads(out.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""


@pytest.mark.skipif(_posix_sh() is None, reason="needs a POSIX sh (Git Bash on Windows)")
def test_wrapper_runs_the_hook_with_a_real_python(tmp_path):
    proj = _project(tmp_path / "project one")
    env = _env(tmp_path, AGENT_MEMORY_PYTHON=sys.executable.replace("\\", "/"))
    text = _hook(env, "SessionStart", {"cwd": str(proj), "session_id": "w1", "source": "startup"}, wrapper=True)
    assert text.startswith('<memory-integration source="agent-memory plugin ') and "MEM on this machine" in text
    del env["CLAUDE_PLUGIN_ROOT"]  # the wrapper also finds the plugin root next to itself
    text = _hook(env, "SessionStart", {"cwd": str(proj), "session_id": "w2", "source": "startup"}, wrapper=True)
    assert "<memory-integration" in text


# ================================================================ printed command, full cycle


def _commands_from(text: str) -> dict[str, str]:
    block = text[text.index("MEM on this machine:"):text.index("</memory-integration>")]
    return dict(re.findall(r"- ([A-Za-z ]+): `([^`]+)`", block))


def _shell_runner(label: str, cmd: str, cwd: Path, env: dict):
    if label == "PowerShell":
        ps = shutil.which("powershell") or "powershell.exe"
        return lambda line: subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                                            f"Set-Location -LiteralPath {plugin.quote_for('powershell', str(cwd))}; "
                                            f"{cmd} {line}"], capture_output=True, env=env, timeout=120)
    sh = shutil.which("bash") if label == "Git Bash" else "sh"
    where = cwd.as_posix() if label == "Git Bash" else str(cwd)
    return lambda line: subprocess.run([sh, "-c", f"cd {plugin.quote_for('bash', where)} && {cmd} {line}"],
                                       capture_output=True, env=env, timeout=120)


def test_full_cycle_with_the_command_printed_by_the_hook(tmp_path):
    env = _env(tmp_path)
    labels = ["Git Bash", "PowerShell"] if sys.platform == "win32" else ["shell"]
    for n, label in enumerate(labels):
        a = _project(tmp_path / f"проект А {n} $x 'q'")
        b = _project(tmp_path / f"project B {n}")
        text = _hook(env, "SessionStart", {"cwd": str(a), "session_id": f"a1-{n}", "source": "startup"})
        cmds = _commands_from(text)
        assert set(cmds) == set(labels) and cmds == {plugin.LABELS[k]: v for k, v in plugin.mem_commands().items()}
        run = _shell_runner(label, cmds[label], a, env)

        def ok(line):
            out = run(line)
            assert out.returncode == 0, (label, line, out.stderr[-400:])
            return out.stdout.decode("utf-8", errors="replace")

        tok = re.search(r"session (s-[0-9a-f]+)", ok('begin "round money"')).group(1)
        diff = tmp_path / f"diff {n}.json"
        diff.write_text(json.dumps({"ops": [{"op": "ADD", "record": {
            "type": "DECISION", "section": "logic", "statement": f"Money is rounded half-up ({label})",
            "evidence": [{"kind": "file", "ref": "app/m.py"}]}}]}), encoding="utf-8")
        q = plugin.quote_for("powershell" if label == "PowerShell" else "bash",
                             diff.as_posix() if label == "Git Bash" else str(diff))
        assert json.loads(ok(f"end --session {tok} --diff {q} --summary done"))["saved"]
        fin = json.loads(ok(f"session finish --session {tok}"))
        assert fin["lease_released_now"] and fin["own_residual_processes"] == []
        shown = _hook(env, "UserPromptSubmit", {"cwd": str(a), "session_id": f"a2-{n}",
                                                "prompt": "how is money rounded here?"})
        assert re.search(rf"- \[logic\] DECISION sourced: Money is rounded half-up \({label}\) \(decision_\d+; "
                         r"sources: file app/m.py \(present\)\)", shown), shown
        other = _hook(env, "UserPromptSubmit", {"cwd": str(b), "session_id": f"b1-{n}",
                                                "prompt": "how is money rounded here?"})
        assert "half-up" not in other and "<project-memory" not in other


# ================================================================ doctor and selftest


def test_doctor_on_this_machine_runs_every_printed_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin data"))
    rep = plugin.doctor(tmp_path)
    runs = [c for c in rep["checks"] if c["check"].startswith("command (")]
    assert rep["ok"], rep["checks"]
    assert len(runs) == (2 if sys.platform == "win32" else 1) and all(c["ok"] for c in runs)
    assert not list(Path(tmp_path).glob("am-doctor-*"))


def test_doctor_output_is_utf8_when_piped(tmp_path):
    cwd = tmp_path / "проект"
    cwd.mkdir()
    env = _env(tmp_path)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    out = subprocess.run([sys.executable, str(ROOT / "bin" / "launcher.py"), "cli", "doctor"], cwd=str(cwd),
                         capture_output=True, env=env, timeout=300)
    data = json.loads(out.stdout.decode("utf-8"))  # bytes are UTF-8, not the console code page
    assert out.returncode == 0 and data["ok"], data["checks"]


def test_selftest_diagnostics_have_no_personal_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin data"))
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "diag.txt"
    assert plugin.selftest(out, run_tests=False) == 0
    text = out.read_text(encoding="utf-8")
    home = os.path.expanduser("~")
    assert "doctor: ok=True" in text and "tests: skipped by the caller" in text
    assert home not in text and home.replace("\\", "/") not in text and str(ROOT) not in text
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    if len(user) > 1:  # never a whole path component or a side of user@host
        assert not re.search(rf"(?i)(?<![\w.-]){re.escape(user)}(?![\w.-])(?=[\\/@])|(?<=[\\/@]){re.escape(user)}"
                             r"(?![\w.-])", text)


def test_user_and_host_names_are_redacted_only_as_whole_names():
    text = ("userdata/user_am.py: 1 user passed, superuser user_x my-user /home/user/x D:\\home\\User\\y "
            "/Users/user user@box /srv/box/ sandbox box.local\n/user")
    out = plugin.redact_name(plugin.redact_name(text, "user", "<user>"), "box", "<host>")
    assert out == ("userdata/user_am.py: 1 user passed, superuser user_x my-user /home/<user>/x D:\\home\\<user>\\y "
                   "/Users/<user> <user>@<host> /srv/<host>/ sandbox box.local\n/<user>")


# ================================================================ Windows -> WSL


DISTRO = os.environ.get("AGENT_MEMORY_TEST_WSL_DISTRO", "Ubuntu")


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("wsl.exe") or not shutil.which("bash"),
                    reason="native Windows with WSL and Git Bash")
def test_project_inside_wsl_is_served_by_the_same_plugin_copy(tmp_path):
    root = "/tmp/am-plugin-" + os.urandom(4).hex()
    proj = f"{root}/проект wsl"
    made = subprocess.run(["wsl.exe", "-d", DISTRO, "--cd", "/", "--exec", "sh", "-c",
                           'mkdir -p "$1/app" && cd "$1" && printf "def f():\\n    return 1\\n" > app/m.py && '
                           "git init -q -b main && git add -A && "
                           "git -c user.name=t -c user.email=t@example.invalid commit -qm init && "
                           "python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' && echo ok",
                           "sh", proj], capture_output=True, timeout=180)
    if b"ok" not in made.stdout:
        pytest.skip(f"WSL distribution {DISTRO} with git and python3.10+ is not available: {made.stderr[-200:]!r}")
    try:
        env = _env(tmp_path, XDG_DATA_HOME=f"{root}/data", WSLENV="XDG_DATA_HOME")
        unc = "\\\\wsl$\\" + DISTRO + proj.replace("/", "\\")
        text = _hook(env, "SessionStart", {"cwd": unc, "session_id": "w1", "source": "startup"})
        cmds = _commands_from(text)
        assert set(cmds) == {"Git Bash", "PowerShell"} and "/bin/launcher.py" in cmds["Git Bash"]
        assert cmds["Git Bash"].endswith(" cli") and "wsl.exe -d " in cmds["PowerShell"]
        assert f"--cd {plugin.quote_for('bash', proj)} --exec" in cmds["Git Bash"]
        bash = shutil.which("bash")

        def ok(line):
            out = subprocess.run([bash, "-c", f"{cmds['Git Bash']} {line}"], capture_output=True, env=env, timeout=120)
            assert out.returncode == 0, (line, out.stderr[-400:])
            return out.stdout.decode("utf-8", errors="replace")

        tok = re.search(r"session (s-[0-9a-f]+)", ok("begin 'check literal $HOME'")).group(1)
        assert "$HOME" in json.loads(ok("status"))["open_episode"]["task"]
        dif = json.dumps({"ops": [{"op": "ADD", "record": {"type": "FACT", "section": "code", "statement":
                                                           "f returns one", "evidence": [{"kind": "file", "ref": "app/m.py"}]}}]})
        subprocess.run(["wsl.exe", "-d", DISTRO, "--cd", "/", "--exec", "sh", "-c", 'cat > "$1"', "sh", f"{root}/d.json"],
                       input=dif.encode(), check=True, timeout=60)
        assert json.loads(ok(f"end --session {tok} --diff {root}/d.json --summary done"))["saved"]
        assert json.loads(ok(f"session finish --session {tok}"))["lease_released_now"]
        shown = _hook(env, "UserPromptSubmit", {"cwd": unc, "session_id": "w2", "prompt": "what does f return"})
        assert "- [code] FACT sourced: f returns one" in shown
    finally:
        if re.fullmatch(r"/tmp/am-plugin-[0-9a-f]{8}", root):
            subprocess.run(["wsl.exe", "-d", DISTRO, "--cd", "/", "--exec", "rm", "-rf", "--", root],
                           capture_output=True, timeout=60)


def test_wsl_time_budget_fits_inside_the_hook_timeout():
    from agent_memory import hook

    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert {g[0]["hooks"][0]["timeout"] for g in hooks.values()} == {hook.HOOK_TIMEOUT}
    assert hook.WSL_BUDGET + 4 <= hook.HOOK_TIMEOUT  # margin for the wrapper and python start-up
    assert hook.MIN_DISPATCH < hook.WSL_BUDGET - hook.WSLPATH_MAX


class _FakeWsl:
    """subprocess.run for wsl.exe on a fake clock: each call takes `costs[i]` seconds or, if that is more
    than the timeout it was given, raises TimeoutExpired exactly at the timeout."""

    def __init__(self, costs, answers):
        self.now, self.costs, self.answers, self.timeouts = 1000.0, list(costs), list(answers), []

    def clock(self):
        return self.now

    def run(self, cmd, **kw):
        t = kw.get("timeout")
        assert t is not None and t > 0, cmd
        self.timeouts.append(t)
        cost = self.costs.pop(0)
        if cost >= t:
            self.now += t
            raise subprocess.TimeoutExpired(cmd, t)
        self.now += cost
        return subprocess.CompletedProcess(cmd, 0, self.answers.pop(0), b"")


@pytest.mark.parametrize("costs,calls", [
    ([60], 1),          # cold WSL: wslpath never answers in time
    ([1, 60], 2),       # wslpath fast, the memory read inside WSL hangs
    ([5.9, 60], 2),     # wslpath just inside its share, the rest of the budget for the read
])
def test_cold_wsl_gives_a_short_hint_inside_the_budget(tmp_path, monkeypatch, costs, calls):
    from agent_memory import hook

    fake = _FakeWsl(costs, [b"/mnt/c/p/bin/launcher.py\n", b""])
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin data"))
    monkeypatch.setattr(plugin.subprocess, "run", fake.run)
    monkeypatch.setattr(hook, "_clock", fake.clock)
    monkeypatch.setattr(hook, "_DEADLINE", fake.now + hook.WSL_BUDGET)
    said = []
    monkeypatch.setattr(hook, "_emit", lambda event, text: said.append(text))
    assert hook._dispatch_to_wsl("UserPromptSubmit", {"prompt": "x"}, "Distro", "/home/x/p", "s1") == 0
    assert len(fake.timeouts) == calls and fake.now - 1000.0 <= hook.WSL_BUDGET + 1e-9
    assert fake.timeouts[0] <= hook.WSLPATH_MAX
    assert said == [f"[project memory] not loaded yet: WSL (Distro) did not answer within {hook.WSL_BUDGET:.0f} s "
                    "(it may still be starting); memory loads with one of the next prompts."]


def test_no_wsl_read_is_started_without_enough_time_left(tmp_path, monkeypatch):
    from agent_memory import hook

    fake = _FakeWsl([1], [b"/mnt/c/p/bin/launcher.py\n"])
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin data"))
    monkeypatch.setattr(plugin.subprocess, "run", fake.run)
    monkeypatch.setattr(hook, "_clock", fake.clock)
    monkeypatch.setattr(hook, "_DEADLINE", fake.now + hook.MIN_DISPATCH + 0.5)  # python start-up was slow
    said = []
    monkeypatch.setattr(hook, "_emit", lambda event, text: said.append(text))
    hook._dispatch_to_wsl("SessionStart", {}, "Distro", "/home/x/p", "s1")
    assert len(fake.timeouts) == 1 and said and said[0].startswith("[project memory] not loaded yet: WSL (Distro)")


# ================================================================ macOS python3 stub


@pytest.mark.skipif(sys.platform == "win32" or not os.path.exists("/usr/bin/python3") or _posix_sh() is None,
                    reason="needs a POSIX system with /usr/bin/python3")
@pytest.mark.parametrize("has_tools", [False, True])
def test_wrapper_never_starts_the_macos_python3_stub(tmp_path, has_tools):
    """macOS without Command Line Tools: /usr/bin/python3 opens an install dialog when started. Here
    `uname` says Darwin and `xcode-select -p` fails, so the wrapper must not start /usr/bin/python3."""
    stub = _stub_dir(tmp_path, {})
    (stub / "uname").write_text("#!/bin/sh\necho Darwin\n", encoding="utf-8")
    (stub / "xcode-select").write_text(f"#!/bin/sh\nexit {0 if has_tools else 2}\n", encoding="utf-8")
    for p in stub.iterdir():
        p.chmod(0o755)
    proj = _project(tmp_path / "proj")
    env = _env(tmp_path, PATH=f"{stub}:/usr/bin:/bin")
    payload = json.dumps({"cwd": str(proj), "session_id": "m1", "source": "startup"}).encode("utf-8")
    out = subprocess.run([_posix_sh(), str(ROOT / "hooks" / "run-hook.sh"), "SessionStart"], input=payload,
                         capture_output=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    said = out.stdout.decode("utf-8")
    if has_tools:  # Command Line Tools present: /usr/bin/python3 is a real python and is started
        assert "macOS stub" not in said
        real = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info >= (3, 10))"],
                              capture_output=True, text=True, timeout=60).stdout.strip()
        assert ("<memory-integration" in said) if real == "True" else ("only an older python" in said)
    else:
        assert "/usr/bin/python3 is only the macOS stub" in json.loads(said)["systemMessage"]


# ================================================================ never wait for input


def test_cli_and_doctor_never_read_stdin(tmp_path):
    """A caller that keeps stdin open (no EOF) must not hang the CLI or doctor."""
    env = _env(tmp_path)
    for args in (["cli", "where"], ["cli", "doctor"]):
        p = subprocess.Popen([sys.executable, str(ROOT / "bin" / "launcher.py"), *args], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(tmp_path))
        try:
            p.wait(timeout=240)  # stdin is never closed
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise AssertionError(f"{args} waited for input")
        finally:
            p.stdin.close()
            p.stdout.close()
            p.stderr.close()
        assert p.returncode in (0, 1), args


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminal (pty) is POSIX-only")
def test_hook_with_a_terminal_as_stdin_returns_at_once(tmp_path):
    import pty

    master, slave = pty.openpty()
    try:
        for argv in ([sys.executable, str(ROOT / "bin" / "launcher.py"), "hook", "SessionStart"],
                     ["sh", str(ROOT / "hooks" / "run-hook.sh"), "SessionStart"]):
            out = subprocess.run(argv, stdin=slave, capture_output=True, env=_env(tmp_path), timeout=60)
            assert out.returncode == 0 and out.stdout == b"", (argv, out.stdout, out.stderr)
    finally:
        os.close(master)
        os.close(slave)
