"""Автоматическое подключение: читающий путь и хук плагина.

Всё — во временных проектах и временном каталоге данных; настройки Claude не используются.
Хук запускается так же, как его запускает Claude Code: отдельным процессом bin/launcher.py
плагина с JSON события на stdin.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_memory.hook import wsl_target
from agent_memory.reader import read_context
from am_helpers import git, write

PKG_ROOT = Path(__file__).resolve().parents[1]
NO_PROBE = ("import sys; import agent_memory.session as s; s.WINDOWS_POWERSHELL = None; s.POSIX_PS = None; "
            "from agent_memory.__main__ import main; sys.exit(main(sys.argv[1:]))")



@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("LOCALAPPDATA", str(data))
    for k in ("AGENT_MEMORY_DIR", "AGENT_MEMORY_REPO", "AGENT_MEMORY_ROOTS", "AGENT_MEMORY_SESSION"):
        monkeypatch.delenv(k, raising=False)
    return home, data


def make_project(path: Path, fn: str) -> Path:
    write(path, "app/mod.py", f"def {fn}():\n    return 1\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def cli(cwd: Path, *args: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AGENT_MEMORY_"))}
    env.update(PYTHONPATH=str(PKG_ROOT), AGENT_MEMORY_ROOTS="app")
    out = subprocess.run([sys.executable, "-c", NO_PROBE, *args], cwd=cwd, env=env, capture_output=True,
                         encoding="utf-8", errors="replace", timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip().startswith(("{", "[")) else {}


def remember(project: Path, section: str, statement: str) -> None:
    tok = cli(project, "session", "start")["session"]
    cli(project, "--session", tok, "commit", json.dumps({"ops": [{"op": "ADD", "record": {
        "type": "DECISION", "section": section, "statement": statement,
        "evidence": [{"kind": "file", "ref": "app/mod.py"}]}}]}))
    cli(project, "--session", tok, "session", "finish")


def tree_state(*roots: Path) -> dict:
    """Файлы и их содержимое. Служебные файлы SQLite (-wal пустой, -shm) — координация чтения
    в режиме WAL, их создаёт сама SQLite при открытии только на чтение; они не данные."""
    out = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.endswith("-shm"):
                if p.name.endswith("-wal") and p.stat().st_size == 0:
                    continue
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ================================================================ читающий путь


def test_reader_changes_nothing_and_missing_memory_is_empty(tmp_path, env_dirs):
    _, data = env_dirs
    empty = make_project(tmp_path / "empty", "nothing")
    before = tree_state(empty / ".git", data)
    res = read_context(empty, "anything at all")
    assert res["status"] == "empty" and res["text"] == ""
    assert tree_state(empty / ".git", data) == before  # нет id-файла, нет хранилища, нет кэша

    proj = make_project(tmp_path / "full", "rotate_secret_key")
    remember(proj, "logic", "Rotation runs nightly at 02:00 for every tenant")
    store_files = tree_state(data)
    res = read_context(proj, "when does rotation run")
    assert res["status"] == "ok" and "02:00" in res["text"] and res["shown"]
    assert tree_state(data) == store_files  # база, метрики, индекс, working.md, аренда — без изменений


def test_reader_sees_committed_wal_data_next_to_an_active_writer(tmp_path, env_dirs):
    from agent_memory import Memory
    from agent_memory.paths import resolve_project

    proj = make_project(tmp_path / "live", "worker")
    store = resolve_project(proj, create=True).store
    mem = Memory(store, proj, roots=("app",))
    try:
        mem.store.k.conn.execute("pragma wal_autocheckpoint=0")
        mem.add({"type": "FACT", "section": "code", "statement": "worker returns one in the live store",
                 "evidence": [{"kind": "file", "ref": "app/mod.py"}]})
        assert (store / "memory.sqlite-wal").stat().st_size > 0  # данные ещё в журнале, не в базе
        writer = sqlite3.connect(str(store / "memory.sqlite"), isolation_level=None, timeout=5)
        writer.execute("begin immediate")  # активная запись другого процесса
        writer.execute("update meta set value=value where key='schema_version'")
        t0 = time.monotonic()
        res = read_context(proj, "worker returns one")
        assert res["status"] == "ok" and "live store" in res["text"]
        assert time.monotonic() - t0 < 3
        writer.execute("rollback")
        writer.close()
    finally:
        mem.close()


def test_wsl_unc_paths_are_translated_not_executed():
    assert wsl_target(r"\\wsl$\Debian\home\alice\proj") == ("Debian", "/home/alice/proj")
    assert wsl_target("//wsl.localhost/Ubuntu-22.04/tmp/a b") == ("Ubuntu-22.04", "/tmp/a b")
    assert wsl_target(r"C:\Data\x") is None and wsl_target("$(rm -rf /)") is None


# ======================================================= хук плагина


def run_hook(data_dir: Path, event: str, payload, env_extra: dict | None = None) -> tuple[int, str, float]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AGENT_MEMORY_", "PYTHONPATH",
                                                                    "CLAUDE_PLUGIN_"))}
    env.update(CLAUDE_PLUGIN_ROOT=str(PKG_ROOT), CLAUDE_PLUGIN_DATA=str(data_dir), **(env_extra or {}))
    t0 = time.monotonic()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    out = subprocess.run([sys.executable, str(PKG_ROOT / "bin" / "launcher.py"), "hook", event],
                         input=raw.encode("utf-8"), capture_output=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr[-400:]
    text = ""
    if out.stdout.strip():
        text = json.loads(out.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    return out.returncode, text, time.monotonic() - t0


def test_plugin_hook_isolates_projects_dedupes_and_respects_opt_out(tmp_path, env_dirs):
    plugin_data = tmp_path / "plugin data"
    one = make_project(tmp_path / "проект один", "alpha_rotation")
    two = make_project(tmp_path / "project two", "beta_billing")
    remember(one, "logic", "Alpha tokens rotate every night")
    remember(two, "data", "Beta invoices are stored as monthly json files")

    code, text, _ = run_hook(plugin_data, "SessionStart", {"cwd": str(one), "session_id": "s1", "source": "startup"})
    assert code == 0 and "Alpha tokens rotate" in text and "Beta invoices" not in text
    assert "read-only" in text and len(text) < 6000
    code, text, _ = run_hook(plugin_data, "UserPromptSubmit",
                             {"cwd": str(two), "session_id": "s2", "prompt": "how are beta invoices stored?"})
    assert "Beta invoices" in text and "Alpha" not in text
    code, again, _ = run_hook(plugin_data, "UserPromptSubmit",
                              {"cwd": str(two), "session_id": "s2", "prompt": "how are beta invoices stored?"})
    assert code == 0 and again == ""  # уже показано в этой сессии — не повторяется
    code, after, _ = run_hook(plugin_data, "SessionStart", {"cwd": str(two), "session_id": "s2", "source": "compact"})
    assert "Beta invoices" in after  # после сжатия контекста нужное снова выдаётся
    code, dup, _ = run_hook(plugin_data, "UserPromptSubmit",
                            {"cwd": str(two), "session_id": "s2", "prompt": "how are beta invoices stored?"})
    assert dup == ""  # и не дублируется следующим запросом

    last = json.loads((plugin_data / "last_hook.json").read_text(encoding="utf-8"))
    assert last["SessionStart"]["output_chars"] > 0 and last["UserPromptSubmit"]["error"] is None
    assert list((plugin_data / "sessions").iterdir())  # кэш сессий — в данных плагина
    (one / ".agent-memory-off").write_text("", encoding="utf-8")
    code, text, _ = run_hook(plugin_data, "SessionStart", {"cwd": str(one), "session_id": "s3", "source": "startup"})
    assert code == 0 and text == ""
    plain = tmp_path / "not a project"
    plain.mkdir()
    for event, payload in (("SessionStart", {"cwd": str(plain)}), ("UserPromptSubmit", {"cwd": str(plain),
                                                                                         "prompt": "x"}),
                           ("Stop", {"cwd": str(one), "stop_hook_active": False}), ("SessionStart", "garbage")):
        code, text, _ = run_hook(plugin_data, event, payload)
        assert code == 0 and text == "", (event, text)


def test_universal_skill_and_roles_carry_no_project_specifics():
    """Скилл и роли плагина ставятся во все проекты: без путей пользователя, адресов и имён
    конкретного проекта."""
    texts = {p.relative_to(PKG_ROOT).as_posix(): p.read_text(encoding="utf-8")
             for p in [*(PKG_ROOT / "skills").rglob("*.md"), *(PKG_ROOT / "agents").glob("*.md")]}
    assert set(texts) == {"skills/project-memory/SKILL.md", "agents/executor.md", "agents/researcher.md",
                          "agents/reviewer.md"}
    # шаблон собран из частей, чтобы проверка утечек в репозитории не находила сам шаблон
    banned = re.compile(r"/" r"home/(?!<)[a-z]|[A-Z]:\\" r"Use" r"rs\\[A-Za-z]|@[a-z0-9-]+\.[a-z]{2,}|https?://")
    for name, text in texts.items():
        assert not banned.search(text), (name, banned.search(text))
    assert "name: project-memory" in texts["skills/project-memory/SKILL.md"]
