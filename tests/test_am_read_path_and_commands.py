"""Читающий путь автоподгрузки, команды установленного CLI и служебная инструкция цикла памяти.

§2: автоподгрузка оценивает записи ТЕМ ЖЕ механизмом, что Memory.get (применимость в этой
рабочей копии и ветке, статус, достоверность сейчас, источники), и ничего не пишет.
§3: команды запуска установленного CLI генерирует установщик из фактической установки; doctor
запускает их ровно этим текстом.
§4: хук выдаёт доверенную служебную инструкцию цикла памяти и при пустой памяти, ничего не создавая.

Все сценарии — временные git-репозитории и хранилища; память владельца не используется.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import agent_memory.hook as hook
from agent_memory import Context, Memory
from agent_memory import plugin
from agent_memory.reader import evaluate, open_readonly, read_context
from am_helpers import git, write

PKG_ROOT = Path(__file__).resolve().parents[1]


def ro(mem):
    """Read-only Memory того же хранилища и той же рабочей копии."""
    return Memory(mem.dir, mem.root, roots=("app", "tests"), read_only=True)


def ctx_text(store: Path, root: Path, query: str | None = None, **kw) -> dict:
    os.environ["AGENT_MEMORY_DIR"] = str(store)
    try:
        return read_context(root, query, **kw)
    finally:
        del os.environ["AGENT_MEMORY_DIR"]


def files_state(store: Path) -> dict:
    out = {}
    for p in sorted(store.rglob("*")):
        if p.is_file() and not p.name.endswith("-shm") and not (p.name.endswith("-wal") and p.stat().st_size == 0):
            out[str(p.relative_to(store))] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


# ============================================================ §2: случаи A и B


def _cases_ab(mem):
    a = mem.add({"type": "FACT", "section": "code", "statement": "Case A comes from a foreign uncommitted patch",
                 "evidence": [{"kind": "file", "ref": "app/auth/service.py"}]})["added"][0]
    b = mem.add({"type": "FACT", "section": "code", "statement": "Case B was stored as verified without a check",
                 "evidence": [{"kind": "file", "ref": "app/auth/service.py"}]})["added"][0]
    with mem.store.transaction():
        mem.store.k.conn.execute("update records set observed_commit=?, observed_scope=?, observed_dirty=1, "
                                 "observed_patch=? where id=?",
                                 ("1" * 40, "other-worktree-abc123", json.dumps({"app/x.py": "deadbeef"}), a))
        mem.store.k.conn.execute("update records set certainty='verified' where id=?", (b,))
    return a, b


def test_case_a_and_b_follow_memory_get(mem):
    a, b = _cases_ab(mem)
    ga, gb = mem.get(a), mem.get(b)
    assert (ga["applicability"], ga["applies_here"]) == ("pending", False)
    assert gb["certainty_here"] == "sourced" and gb["certainty"] == "verified"
    res = ctx_text(mem.dir, mem.root, "case foreign patch stored verified check")
    assert "Case A" not in res["text"] and res["hidden"] >= 1  # не выдаётся как факт
    assert "FACT sourced: Case B" in res["text"] and "verified" not in res["text"].split("Case B")[0][-30:]
    assert "sources: file app/auth/service.py" in res["text"]


# ======================================================= §2: настоящие состояния git


def _fact(statement, ref="app/auth/service.py", **extra):
    return {"ops": [{"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": statement,
                                            "evidence": [{"kind": "file", "ref": ref}], **extra}}]}


def test_two_worktrees_foreign_uncommitted_then_merged(two_worktrees, tmp_path):
    main, wt_b = two_worktrees
    store = tmp_path / "shared"
    mem_a, mem_b = Memory(store, main, roots=("app", "tests")), Memory(store, wt_b, roots=("app", "tests"))
    try:
        ctx_b = Context(mem_b)
        ctx_b.start_session("b")
        write(wt_b, "app/auth/extra.py", "def extra_rule():\n    return 'b'\n")  # не закоммичено
        ctx_b.begin("extra rule")
        rid = ctx_b.end(_fact("extra_rule returns b", ref="app/auth/extra.py"))["commit"]["added"][0]
        assert mem_a.get(rid)["applies_here"] is False and mem_b.get(rid)["applies_here"] is True
        assert "extra_rule" not in ctx_text(store, main, "extra rule returns")["text"]
        assert "extra_rule returns b" in ctx_text(store, wt_b, "extra rule returns")["text"]
        git(wt_b, "add", "-A")
        git(wt_b, "commit", "-qm", "extra rule")
        mem_b.anchor_pending()
        assert "extra_rule" not in ctx_text(store, main, "extra rule returns")["text"]  # ветка ещё не влита
        git(main, "merge", "-q", "--no-edit", "feature")
        assert mem_a.get(rid)["applies_here"] is True
        assert "extra_rule returns b" in ctx_text(store, main, "extra rule returns")["text"]
        git(main, "reset", "-q", "--hard", "HEAD~1")  # откат во временном репозитории теста
        assert "extra_rule" not in ctx_text(store, main, "extra rule returns")["text"]
    finally:
        mem_a.close()
        mem_b.close()


def test_invalidation_on_a_branch_does_not_close_the_fact_on_main(repo, tmp_path):
    store = tmp_path / "store"
    mem = Memory(store, repo, roots=("app", "tests"))
    rid = mem.add({"type": "FACT", "section": "logic", "statement": "Tokens rotate on every refresh",
                   "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    git(repo, "commit", "-q", "--allow-empty", "-m", "anchor")
    mem.anchor_pending()
    wt = tmp_path / "wt-side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    mem_side = Memory(store, wt, roots=("app", "tests"))
    try:
        git(wt, "commit", "-q", "--allow-empty", "-m", "side diverges")  # переход — на коммите только ветки
        mem_side.invalidate(rid, "not true on this branch", expected_version=mem_side.get(rid)["version"])
        assert mem_side.get(rid)["status_here"] == "invalidated" and mem.get(rid)["status_here"] == "active"
        assert "Tokens rotate" not in ctx_text(store, wt, "tokens rotate refresh")["text"]
        assert "Tokens rotate on every refresh" in ctx_text(store, repo, "tokens rotate refresh")["text"]
    finally:
        mem_side.close()
        mem.close()


SIMPLE = "tests/test_simple.py::test_adds"


def test_verified_needs_a_valid_check_and_drops_when_test_or_source_changes(repo, tmp_path):
    store = tmp_path / "store"
    mem = Memory(store, repo, roots=("app", "tests"))
    mem.index()
    rid = mem.add({"type": "FACT", "section": "code", "statement": "Addition works in the demo suite",
                   "evidence": [{"kind": "check", "method": "pytest", "ref": SIMPLE, "expect": "pass",
                                 "condition": "the addition test passes"}]})["added"][0]
    q = "addition works demo suite"
    assert mem.get(rid)["certainty_here"] == "verified"
    assert "FACT verified: Addition works" in ctx_text(store, repo, q)["text"]
    test_file = repo / "tests" / "test_simple.py"
    test_file.write_text(test_file.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert mem.get(rid)["certainty_here"] == "needs_recheck"
    assert "FACT needs_recheck: Addition works" in ctx_text(store, repo, q)["text"]
    test_file.unlink()
    level = mem.get(rid)["certainty_here"]
    assert level != "verified" and f"FACT {level}: Addition works" in ctx_text(store, repo, q)["text"]
    mem.close()


def test_missing_code_map_is_not_confirmation(repo, tmp_path):
    store = tmp_path / "store"
    mem = Memory(store, repo, roots=("app", "tests"))
    mem.index()
    rid = mem.add({"type": "FACT", "section": "code", "statement": "rotate_token issues a new token",
                   "evidence": [{"kind": "code", "ref": "code:app/auth/service.py::AuthService.rotate_token"}]})[
        "added"][0]
    mem.close()
    shutil.rmtree(store / "worktrees")  # карты кода этой копии нет
    res = ctx_text(store, repo, "rotate token issues new token")
    assert "no code map for this worktree" in res["text"]
    assert "(missing)" in res["text"] and rid in res["text"]


# ========================================================== §2: паритет и чтение без записи


def test_parity_between_memory_get_and_the_read_path(mem, put, auth_source):
    a, b = _cases_ab(mem)
    c = mem.add({"type": "DECISION", "section": "logic", "statement": "Rotation must be atomic",
                 "evidence": [{"kind": "code", "ref": "code:app/auth/service.py::AuthService.rotate_token"}]})[
        "added"][0]
    d = mem.add({"type": "FACT", "section": "data", "statement": "Docs describe rotation",
                 "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    mem.index()
    put("app/auth/service.py", auth_source.replace("return new_token", "return new_token  # changed"))
    mem.index()
    reader_mem = ro(mem)
    try:
        for rid in (a, b, c, d):
            want = mem.get(rid)
            got = evaluate(reader_mem, rid)
            assert (got["applicability"], got["applies_here"], got["status_here"], got["certainty_here"]) == \
                (want["applicability"], want["applies_here"], want["status_here"], want["certainty_here"]), rid
            want_src = [(s["kind"], s.get("current_ref") or s["ref"], s["verification"]) for s in mem.sources(rid)]
            got_src = [(s["kind"], s["ref"], s["state"].split(",")[0]) for s in got["sources"]]
            assert got_src == want_src[:4], rid
    finally:
        reader_mem.close()


def test_the_read_path_writes_nothing(mem, ctx):
    ctx.begin("some work")
    ctx.note("a note")
    mem.add({"type": "FACT", "section": "code", "statement": "AuthService has rotate_token",
             "evidence": [{"kind": "file", "ref": "app/auth/service.py"}]})
    mem.store.k.conn.execute("pragma wal_checkpoint(TRUNCATE)")
    before = files_state(mem.dir)
    res = ctx_text(mem.dir, mem.root, "rotate token auth service")
    assert res["status"] == "ok"
    assert files_state(mem.dir) == before  # знания, аренда, эпизоды, метрики, индекс, working.md — без изменений
    reader_mem = ro(mem)
    try:
        with pytest.raises(Exception):  # запись через читающий объект невозможна в принципе
            reader_mem.store.k.conn.execute("insert into meta(key, value) values('x', 'y')")
    finally:
        reader_mem.close()


def test_missing_or_other_schema_store_is_not_created(tmp_path, repo):
    store = tmp_path / "nothing-here"
    with pytest.raises(Exception):
        open_readonly(type("P", (), {"store": store, "root": repo})(), 1.0)
    assert not store.exists()


# ============================================================= §2: повтор и изменение


def _hook_out(event: str, payload: dict) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        hook.run(event, payload)
    raw = buf.getvalue()
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw.strip() else ""


def test_changed_fact_or_certainty_is_shown_again_unchanged_is_not(mem, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(mem.dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin data"))
    rid = mem.add({"type": "FACT", "section": "logic", "statement": "Refresh revokes the old token",
                   "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    p = {"cwd": str(mem.root), "session_id": "s1", "prompt": "does refresh revoke the old token"}
    assert "Refresh revokes the old token" in _hook_out("UserPromptSubmit", p)
    assert _hook_out("UserPromptSubmit", p) == ""  # неизменное не повторяется
    mem.update(rid, {"reason": "confirmed by the auth doc"}, expected_version=mem.get(rid)["version"])
    assert "Refresh revokes the old token" in _hook_out("UserPromptSubmit", p)  # версия изменилась — снова
    mem.invalidate(rid, "no longer true", expected_version=mem.get(rid)["version"])
    out = _hook_out("UserPromptSubmit", p)
    assert "Refresh revokes the old token" not in out  # отозванное не выдаётся как факт


# ===================================================== §4: служебная инструкция цикла


def test_empty_project_gets_the_trusted_cycle_note_and_nothing_is_created(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("LOCALAPPDATA", str(data))
    monkeypatch.delenv("AGENT_MEMORY_DIR", raising=False)
    proj = tmp_path / "новый проект"
    write(proj, "app/m.py", "def f():\n    return 1\n")
    git(proj, "init", "-q", "-b", "main")
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "init")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin data"))
    out = _hook_out("SessionStart", {"cwd": str(proj), "session_id": "e1", "source": "startup"})
    assert out.startswith(f'<memory-integration source="agent-memory plugin {plugin.version()}" trusted="true">')
    assert all(cmd in out for cmd in plugin.mem_commands().values()) and "<project-memory" not in out
    assert "agent-memory:executor" in out
    assert "session finish" in out and "--operator-confirmed" in out and "end of a response" in out
    assert not (proj / ".git" / "agent-memory.json").exists()  # ни id, ни хранилища
    assert not (data / "agent-memory" / "projects").exists()
    p = {"cwd": str(proj), "session_id": "e1", "prompt": "add a helper"}
    assert _hook_out("UserPromptSubmit", p) == ""  # инструкция уже дана в этой сессии
    assert "<memory-integration" in _hook_out("UserPromptSubmit", {**p, "session_id": "e2"})  # SessionStart не было
    assert "<memory-integration" in _hook_out("SessionStart", {**p, "session_id": "e1", "source": "compact"})
    assert _hook_out("SessionStart", {"cwd": str(proj), "session_id": "e3", "source": "startup",
                                      "agent_memory_dispatched": True}) == ""  # инструкцию даёт Windows-сторона
    (proj / ".agent-memory-off").write_text("", encoding="utf-8")
    assert _hook_out("SessionStart", {"cwd": str(proj), "session_id": "e4", "source": "startup"}) == ""
    assert _hook_out("SessionStart", {"session_id": "e5"}) == ""  # без cwd каталог не угадывается


def test_wsl_commands_are_filled_with_the_project_path():
    state = {"commands": {"wsl_from_git_bash": "MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu --cd <WSL_PROJECT> --exec "
                                               "python3 /x/launcher.py cli",
                          "wsl_from_powershell": "wsl.exe -d Ubuntu --cd <WSL_PROJECT> --exec python3 /x/launcher.py cli"}}
    got = dict(hook._commands_for(state, "/home/u/мой проект"))
    assert got["Git Bash"] == "MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu --cd '/home/u/мой проект' --exec python3 /x/launcher.py cli"
    assert "--cd '/home/u/мой проект'" in got["PowerShell"]
    # $, обратная кавычка, апостроф: в одинарных кавычках своей оболочки ничего не раскрывается
    nasty = "/home/u/p $HOME `id` 'q'"
    got = dict(hook._commands_for(state, nasty))
    assert "--cd '/home/u/p $HOME `id` '\"'\"'q'\"'\"'' --exec" in got["Git Bash"]
    assert "--cd '/home/u/p $HOME `id` ''q''' --exec" in got["PowerShell"]
    # двойную кавычку PowerShell 5.1 передаёт внешней программе искажённо: только команда Git Bash
    got = dict(hook._commands_for(state, '/home/u/a "b"'))
    assert set(got) == {"Git Bash"} and """--cd '/home/u/a "b"' --exec""" in got["Git Bash"]
    # команды прежних версий (с "<WSL_PROJECT>") тоже заполняются безопасно
    old = {"commands": {"wsl_from_git_bash": 'wsl.exe -d Ubuntu --cd "<WSL_PROJECT>" --exec python3 /x/l.py cli'}}
    assert dict(hook._commands_for(old, "/home/u/$x"))["Git Bash"] == "wsl.exe -d Ubuntu --cd '/home/u/$x' --exec python3 /x/l.py cli"


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("bash"), reason="native Windows with Git Bash")
def test_quoting_survives_the_real_shells_with_a_hostile_path(tmp_path):
    """Настоящие Git Bash и PowerShell: аргумент, собранный quote_for, доходит до программы байт в байт."""
    echo = tmp_path / "echo.py"
    echo.write_text("import sys, json\nsys.stdout.buffer.write(json.dumps(sys.argv[1:]).encode())\n", encoding="utf-8")
    bash, ps = shutil.which("bash"), shutil.which("powershell") or "powershell.exe"
    py = sys.executable.replace("\\", "/")
    for value in ("/tmp/проект $HOME `id` 'q'", '/tmp/a "b" $x', "C:/Data/a b/x'y"):
        # как в настоящей команде wsl_from_git_bash: MSYS_NO_PATHCONV=1, иначе Git Bash переписывает /tmp/... в C:/...
        line = (f"MSYS_NO_PATHCONV=1 {plugin.quote_for('bash', py)} {plugin.quote_for('bash', echo.as_posix())} "
                f"{plugin.quote_for('bash', value)}")
        out = subprocess.run([bash, "-c", line], capture_output=True, timeout=60)
        assert json.loads(out.stdout.decode("utf-8")) == [value], (value, out.stderr[-300:])
        if '"' in value:
            continue
        line = f"& {plugin.quote_for('powershell', sys.executable)} {plugin.quote_for('powershell', str(echo))} " \
               f"{plugin.quote_for('powershell', value)}"
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", line], capture_output=True, timeout=60)
        assert json.loads(out.stdout.decode("utf-8")) == [value], (value, out.stderr[-300:])
