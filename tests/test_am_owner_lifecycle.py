"""Жизнь владельца аренды: владелец — процесс Claude Code, а не фоновый держатель.

Владелец записывается при `session start`/`begin`: CLI сам проходит по цепочке родителей
Windows до ближайшего claude.exe. dead — только если этот процесс доказанно завершился
(pid исчез или занят другим процессом); всё остальное unknown с кодом причины, и тогда
аренду передаёт только человек (`--operator-confirmed`). Конец задачи — `session finish`.

Тесты с фикстурой claude_host идут по настоящему пути Windows -> WSL: настоящий
powershell.exe, настоящий процесс Claude Code этой сессии, настоящий заменитель хоста
(PING.EXE), который тест сам запускает и сам останавливает. Без связи с Windows или вне
Claude Code они пропускаются с причиной — такой пропуск не считается проверкой.
Процессы других сессий тесты не трогают.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import agent_memory.session as session_mod
from agent_memory.session import find_host, owner_state

REAL_PS = session_mod.WINDOWS_POWERSHELL  # до автоматической подмены в conftest
_HOST: dict = {}


def _ps(script: str) -> str:
    out = subprocess.run([REAL_PS, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                         stdin=subprocess.DEVNULL, timeout=60)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    return out.stdout.decode("utf-8", "replace")


@pytest.fixture
def claude_host(monkeypatch) -> dict:
    """Процесс Claude Code, из которого запущены тесты (настоящий захват через powershell)."""
    if not REAL_PS or not Path(REAL_PS).exists():
        pytest.skip("needs WSL interop with Windows (powershell.exe)")
    monkeypatch.setattr(session_mod, "WINDOWS_POWERSHELL", REAL_PS)
    if "rec" not in _HOST:
        _HOST["rec"] = session_mod.capture_owner()
    rec = _HOST["rec"]
    if rec.get("kind") != "claude-host":
        pytest.skip(f"tests are not running under Claude Code on Windows: {rec}")
    return rec


def _db(store: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(store / "memory.sqlite"))


def _set_lease(store: Path, **cols) -> None:
    db = _db(store)
    for col, value in cols.items():
        db.execute(f"update leases set {col}=?", (value if value is None else json.dumps(value),))
    db.commit()
    db.close()


def _lease_row(store: Path) -> tuple | None:
    db = _db(store)
    row = db.execute("select token, owner_proc from leases").fetchone()
    db.close()
    return row


def _owner(cli) -> dict:
    return cli.json("session", "status", session=False)["lease"]["owner"]


def _standin() -> dict:
    """Настоящий процесс Windows вместо Claude Code: его тест запускает и останавливает сам."""
    out = _ps('$p = Start-Process -FilePath "$env:WINDIR\\System32\\PING.EXE" -ArgumentList "-n","600",'
              '"127.0.0.1" -WindowStyle Hidden -PassThru; $c = Get-CimInstance Win32_Process -Filter '
              '"ProcessId=$($p.Id)"; "{0}`t{1}`t{2}" -f $p.Id,$c.CreationDate.ToFileTimeUtc(),$env:COMPUTERNAME')
    pid, created, computer = out.strip().split("\t")
    return {"kind": "claude-host", "name": "PING.EXE (stand-in)", "pid": int(pid), "created": created,
            "computer": computer, "captured_at": "test", "via": "test stand-in"}


def _stop_standin(pid: int) -> None:
    subprocess.run([REAL_PS, "-NoProfile", "-NonInteractive", "-Command",
                    f"Stop-Process -Id {int(pid)} -Force -ErrorAction SilentlyContinue"],
                   capture_output=True, stdin=subprocess.DEVNULL, timeout=60)


def _fake_holder(token: str, via_env: bool = False) -> subprocess.Popen:
    """Процесс с командной строкой держателя прежней версии (`agent_memory session hold`)."""
    argv = [sys.executable, "-c", "import time; time.sleep(300)", "agent_memory", "session", "hold"]
    env = dict(os.environ)
    if via_env:
        env["AGENT_MEMORY_SESSION"] = token
    else:
        argv += ["--session", token]
    proc = subprocess.Popen(argv, env=env)
    time.sleep(0.3)
    return proc


def _registered(pid: int, start: str | None = None) -> dict:
    ident = session_mod.proc_identity(pid)
    return {**ident, "start": start or ident["start"], "registered_at": "test"}


def _archive_notes(store: Path) -> list[str]:
    archive = json.loads((store / "episodes" / "000001.json").read_text(encoding="utf-8"))
    return [n["text"] for n in archive["notes"]]


# ============================================================ без Windows


def test_session_hold_is_retired(repo, cli_factory, tmp_path):
    cli = cli_factory(repo, tmp_path / "store")
    cli.start()
    out = cli.run("session", "hold", timeout=30)
    assert out.returncode == 2 and "retired" in out.stderr and "session finish" in out.stderr
    assert session_mod._holder_processes(cli.session) == []


def test_writes_never_query_windows(ctx, monkeypatch):
    def forbidden(script):
        raise AssertionError("a write queried Windows")

    monkeypatch.setattr(session_mod, "_powershell", forbidden)
    ctx.begin("task")
    ctx.note("note")
    assert ctx.end(None, summary="done")["saved"]


def test_owner_state_codes_and_host_search(monkeypatch):
    assert owner_state(None)["code"] == "no_owner_captured"
    assert owner_state(None, {"pid": 1})["code"] == "legacy_holder_record"
    assert owner_state({"kind": "uncaptured", "code": "no_interop", "detail": "x"})["code"] == "no_interop"
    rec = {"kind": "claude-host", "name": "claude.exe", "pid": 1234, "created": "100", "computer": "PC"}
    assert owner_state(rec)["code"] == "no_interop"  # conftest: powershell недоступен
    # ПОДМЕНА ответа PowerShell (не настоящий сбой ОС); полная таблица — test_am_owner_trust.py
    f = "AMV1 BEGIN\n{}\nAMV1 END\n".format
    answers = {
        f("HOST\tPC\nPROC\tABSENT\t1234"): ("dead", "host_gone"),
        f("HOST\tPC\nPROC\tPRESENT\t1234\tclaude.exe\t200"): ("dead", "host_reused"),
        f("HOST\tPC\nPROC\tPRESENT\t1234\tclaude.exe\t100"): ("unknown", "host_alive_agent_unobservable"),
        f("HOST\tOTHER\nPROC\tABSENT\t1234"): ("unknown", "other_host"),
        "garbage": ("unknown", "query_failed"),
    }
    for text, expected in answers.items():
        monkeypatch.setattr(session_mod, "_powershell", lambda script, t=text: ("ok", t))
        got = owner_state(rec)
        assert (got["state"], got["code"]) == expected, text
    monkeypatch.setattr(session_mod, "_powershell", lambda script: ("query_failed", "boom"))
    assert owner_state(rec)["code"] == "query_failed"
    assert owner_state({"kind": "uncaptured", "code": "x"})["state"] == "unknown"

    rows = {1: {"pid": 1, "ppid": 0, "name": "sihost.exe", "created": 1},
            2: {"pid": 2, "ppid": 1, "name": "claude.exe", "created": 2},      # приложение
            3: {"pid": 3, "ppid": 2, "name": "claude.exe", "created": 3},      # Claude Code CLI
            4: {"pid": 4, "ppid": 3, "name": "bash.exe", "created": 4},
            5: {"pid": 5, "ppid": 4, "name": "powershell.exe", "created": 5}}
    assert find_host(rows, 5)["pid"] == 3  # ближайший claude.exe, не оболочка приложения
    rows[4]["created"] = 9  # родитель моложе потомка: pid занят новым процессом — цепочка рвётся
    assert find_host(rows, 5) is None
    assert find_host({5: {"pid": 5, "ppid": 77, "name": "powershell.exe", "created": 5}}, 5) is None


@pytest.mark.skipif(sys.platform == "win32", reason="legacy holder processes and sh/SIGKILL are POSIX-only")
def test_finish_refuses_open_episode_and_is_repeatable_after_interruption(repo, cli_factory, tmp_path):
    store = tmp_path / "store"
    owner = cli_factory(repo, store)
    owner.start()
    assert owner.run("begin", "task").returncode == 0
    owner.json("note", "work")
    out = owner.run("session", "finish")
    assert out.returncode == 4 and "still open" in out.stderr and "whole task" in out.stderr
    owner.json("end", "--summary", "done")
    by_cmd, by_env = _fake_holder(owner.session), _fake_holder(owner.session, via_env=True)
    other = _fake_holder("s-another-session")
    try:
        # finish оборван сразу после снятия аренды (kill -9): держатели ещё живы
        out = owner.run("session", "finish", fault="finish_after_release:exit")
        assert out.returncode == 91 and _lease_row(store) is None
        assert by_cmd.poll() is None and by_env.poll() is None
        proof = owner.json("session", "finish")  # повтор доводит дело
        assert proof["lease_released_now"] is False and proof["lease_now"] is None
        assert {p["pid"] for p in proof["legacy_holders_stopped"]} == {by_cmd.pid, by_env.pid}
        assert proof["own_residual_processes"] == [] and proof["new_start_possible"]
        assert by_cmd.wait(10) is not None and by_env.wait(10) is not None
        assert other.poll() is None  # чужой токен не тронут
        nxt = cli_factory(repo, store)
        nxt.start()
        again = owner.json("session", "finish")  # ещё раз, когда копию уже взяла новая задача
        assert again["lease_now"] == {"token": nxt.session} and not again["new_start_possible"]
        assert again["legacy_holders_stopped"] == [] and _lease_row(store)[0] == nxt.session
    finally:
        for p in (by_cmd, by_env, other):
            p.kill()
            p.wait(10)


def test_finish_is_truthful_for_other_tokens_and_open_episodes(repo, cli_factory, tmp_path):
    store = tmp_path / "store"
    owner = cli_factory(repo, store)
    owner.start()
    out = owner.run("session", "finish", "--session", "s-deadbeef", session=False)  # никогда не владел
    assert out.returncode == 4 and "does not own" in out.stderr and _lease_row(store)[0] == owner.session
    proof = owner.json("session", "finish")
    assert proof["lease_released_now"] and proof["open_episode"] is None and proof["new_start_possible"]
    out = owner.run("session", "finish", "--session", "s-deadbeef", session=False)  # копия свободна
    assert out.returncode == 4 and "has no lease" in out.stderr
    nxt = cli_factory(repo, store)
    nxt.start()
    assert nxt.run("begin", "next task").returncode == 0
    again = owner.json("session", "finish")  # тот же токен повторно: правдивое состояние копии
    assert again["already_finished_before"] and not again["lease_released_now"]
    assert again["lease_now"] == {"token": nxt.session}
    assert again["open_episode"] == {"id": "episode_000001", "owner_token": nxt.session}
    assert not again["new_start_possible"]


@pytest.mark.skipif(sys.platform == "win32", reason="legacy holder processes and sh/SIGKILL are POSIX-only")
def test_finish_never_signals_a_reused_pid(repo, cli_factory, tmp_path):
    store = tmp_path / "store"
    owner = cli_factory(repo, store)
    owner.start()
    unrelated = subprocess.Popen(["sleep", "300"])
    mine = _fake_holder(owner.session, via_env=True)
    try:
        # запись держателя указывает на pid постороннего процесса, но время старта другое
        _set_lease(store, holder_proc=_registered(unrelated.pid, start="1"))
        proof = owner.json("session", "finish")
        assert proof["lease_released_now"] and [p["pid"] for p in proof["legacy_holders_stopped"]] == [mine.pid]
        assert unrelated.poll() is None and proof["own_residual_processes"] == []
    finally:
        for p in (unrelated, mine):
            p.kill()
            p.wait(10)


def test_early_v2_store_with_holder_record_is_unknown_after_upgrade(repo, cli_factory, tmp_path):
    """Аренда прежней сборки (только holder_proc, без owner_proc): столбец добавляется при
    открытии, состояние — unknown/legacy_holder_record, данные целы. Проверка на копии."""
    store = tmp_path / "store"
    owner = cli_factory(repo, store)
    token = owner.start()
    owner.json("commit", json.dumps({"ops": [{"op": "ADD", "record": {
        "type": "FACT", "section": "code", "statement": "README names the demo", "evidence": [{"kind": "file", "ref": "README.md"}]}}]}))
    copy = tmp_path / "store-copy"
    shutil.copytree(store, copy)
    db = _db(copy)
    db.executescript("""
        create table leases_old(scope text primary key, token text not null, holder text,
            acquired_at text not null, heartbeat_at text not null, ttl_s integer not null, previous_token text,
            holder_proc text, history text);
        insert into leases_old select scope, token, holder, acquired_at, heartbeat_at, ttl_s, previous_token,
            '{"host": "h", "pid": 1, "start": "1"}', history from leases;
        drop table leases;
        alter table leases_old rename to leases;
    """)
    db.close()
    early = cli_factory(repo, copy)
    early.session = token
    lease = early.json("session", "status", session=False)["lease"]
    assert lease["token"] == token and lease["owner"]["state"] == "unknown"
    assert lease["owner"]["code"] == "legacy_holder_record"
    assert "owner_proc" in {r[1] for r in _db(copy).execute("pragma table_info(leases)")}
    out = cli_factory(repo, copy).run("session", "recover", session=False)
    assert out.returncode == 4 and "never supplies" in out.stderr
    assert early.json("search", "README names")[0]["statement"] == "README names the demo"
    assert early.run("begin", "after upgrade").returncode == 0


# ===================================================== настоящий путь Windows -> WSL


def test_computer_name_is_not_taken_from_the_environment(repo, cli_factory, tmp_path, claude_host):
    """COMPUTERNAME, проброшенный из WSL через WSLENV, не меняет имя компьютера владельца."""
    cli = cli_factory(repo, tmp_path / "store", interop=True)
    cli.env.update(WSLENV="COMPUTERNAME", COMPUTERNAME="FAKEPC")
    cli.start()
    lease = cli.json("session", "status", session=False)["lease"]
    assert lease["owner_proc"]["computer"] == claude_host["computer"] != "FAKEPC"
    assert lease["owner"]["code"] == "host_alive_agent_unobservable"  # не other_host


def test_owner_is_the_claude_code_process_captured_at_start(repo, cli_factory, tmp_path, claude_host):
    cli = cli_factory(repo, tmp_path / "store", interop=True)
    cli.start()
    lease = cli.json("session", "status", session=False)["lease"]
    assert lease["owner_proc"]["pid"] == claude_host["pid"]
    assert lease["owner_proc"]["created"] == claude_host["created"]
    assert lease["owner_proc"]["name"].lower() == "claude.exe" and lease["holder_proc"] is None
    owner = lease["owner"]
    assert owner["state"] == "unknown" and owner["code"] == "host_alive_agent_unobservable"
    assert owner["host"]["state"] == "alive" and owner["host"]["pid"] == claude_host["pid"]


@pytest.mark.skipif(sys.platform == "win32", reason="legacy holder processes and sh/SIGKILL are POSIX-only")
def test_live_owner_keeps_the_lease_through_holder_crash_silence_and_shell_exit(repo, cli_factory, tmp_path,
                                                                                 claude_host):
    store = tmp_path / "store"
    owner = cli_factory(repo, store, interop=True)
    owner.start()
    assert owner.run("begin", "long task").returncode == 0
    owner.json("note", "owner note")
    holder = _fake_holder(owner.session)  # держатель прежней версии, записанный в аренду
    _set_lease(store, holder_proc=_registered(holder.pid))
    holder.send_signal(signal.SIGKILL)  # держатель упал, владелец продолжает работу
    holder.wait(10)
    time.sleep(2)  # тишина: ни одной команды памяти
    env = dict(owner.env, AGENT_MEMORY_SESSION=owner.session)
    short = subprocess.run(["sh", "-c", shlex.join(owner.argv("note", "from a short shell")) + "; exit 0"],
                           env=env, capture_output=True, text=True, timeout=120)
    assert short.returncode == 0, short.stderr  # короткая оболочка команды завершилась

    second = cli_factory(repo, store, interop=True)
    state = _owner(second)
    assert state["state"] == "unknown" and state["code"] == "host_alive_agent_unobservable"
    assert state["host"]["state"] == "alive"
    out = second.run("session", "recover", session=False)
    assert out.returncode == 4 and "unknown" in out.stderr and "human" in out.stderr
    assert second.run("session", "start", session=False).returncode == 4
    assert owner.json("note", "owner still writes")["seq"] == 3
    assert owner.json("end", "--summary", "done")["saved"]
    proof = owner.json("session", "finish")
    assert proof["lease_released_now"] and proof["own_residual_processes"] == []
    assert _archive_notes(store) == ["owner note", "from a short shell", "owner still writes"]


def test_end_of_a_response_is_not_the_end_of_the_task(repo, cli_factory, tmp_path, claude_host):
    store = tmp_path / "store"
    owner = cli_factory(repo, store, interop=True)
    owner.start()
    assert owner.run("begin", "implement, then wait for review").returncode == 0
    owner.json("note", "implemented; waiting for the reviewer")
    # Ответ исполнителя закончился: все его команды завершились, фоновых процессов нет.
    assert session_mod._holder_processes(owner.session) == []
    second = cli_factory(repo, store, interop=True)
    assert second.run("session", "start", session=False).returncode == 4
    assert second.run("session", "recover", session=False).returncode == 4
    lease = second.json("session", "status", session=False)["lease"]
    assert lease["token"] == owner.session and lease["owner"]["host"]["state"] == "alive"
    # Продолжение после ревью: тот же исполнитель, тот же токен, тот же эпизод.
    assert owner.json("note", "review fix applied")["seq"] == 2
    assert owner.json("end", "--summary", "delivered")["saved"]
    assert owner.json("session", "finish")["new_start_possible"]
    assert second.start()  # следующая задача получает копию
    assert _archive_notes(store) == ["implemented; waiting for the reviewer", "review fix applied"]


def test_proven_dead_claude_process_allows_recover_and_rejects_the_old_token(repo, cli_factory, tmp_path,
                                                                              claude_host):
    store = tmp_path / "store"
    owner = cli_factory(repo, store, interop=True)
    owner.start()
    assert owner.run("begin", "owner task").returncode == 0
    owner.json("note", "owner note")
    stand = _standin()
    try:
        _set_lease(store, owner_proc=stand)  # владелец — заменитель, который тест остановит сам
        second = cli_factory(repo, store, interop=True)
        alive = _owner(second)
        assert alive["state"] == "unknown" and alive["host"]["state"] == "alive"
        _stop_standin(stand["pid"])
        dead = _owner(second)
        assert dead["state"] == "dead" and dead["code"] == "host_gone"
        out = second.run("session", "recover", session=False, fault="recover_commit:exit")
        assert out.returncode == 91 and _lease_row(store)[0] == owner.session  # оборванная передача — без следа
        rec = second.json("session", "recover", session=False)
        assert rec["previous_token"] == owner.session and rec["adopted_episode"] == "episode_000001"
        assert rec["owner_state"]["code"] == "host_gone" and rec["owner"]["pid"] == claude_host["pid"]
        second.session = rec["token"]
        assert owner.run("note", "zombie write").returncode == 4
        assert owner.run("end", "--summary", "zombie end").returncode == 4
        old = owner.run("session", "finish")  # старый токен не владеет и не завершал: отказ
        assert old.returncode == 4 and "does not own" in old.stderr
        history = second.json("session", "status", session=False)["lease"]["history"]
        assert history[-1]["from"] == owner.session and history[-1]["code"] == "host_gone"
        assert second.json("end", "--summary", "finished after recovery")["saved"]
        notes = _archive_notes(store)
        assert notes[0] == "owner note" and "took over" in notes[1] and owner.session in notes[1]
        assert second.json("session", "finish")["lease_released_now"]
    finally:
        _stop_standin(stand["pid"])


def test_reused_pid_counts_as_exited(repo, cli_factory, tmp_path, claude_host):
    store = tmp_path / "store"
    owner = cli_factory(repo, store, interop=True)
    owner.start()
    # pid живого процесса Claude Code, но другое время создания: записанный владелец завершился
    _set_lease(store, owner_proc={**claude_host, "created": str(int(claude_host["created"]) - 10_000_000)})
    state = _owner(cli_factory(repo, store, interop=True))
    assert state["state"] == "dead" and state["code"] == "host_reused"


def test_unknown_owner_needs_the_human_and_records_the_reason(repo, cli_factory, tmp_path, claude_host,
                                                             monkeypatch):
    store = tmp_path / "store"
    owner = cli_factory(repo, store, interop=True)
    owner.start()
    assert owner.run("begin", "owner task").returncode == 0
    owner.json("note", "owner note")
    _set_lease(store, owner_proc={**claude_host, "computer": "ANOTHER-PC"})
    second = cli_factory(repo, store, interop=True)
    assert _owner(second)["code"] == "other_host"
    out = second.run("session", "recover", session=False)
    assert out.returncode == 4 and "human" in out.stderr and "never supplies" in out.stderr
    out = second.run("session", "recover", "--operator-confirmed", "  ", session=False)
    assert out.returncode == 4 and "reason from the human" in out.stderr
    reason = "owner confirmed in chat: that task was cancelled"
    rec = second.json("session", "recover", "--operator-confirmed", reason, session=False)
    assert rec["operator_confirmed"] == reason and rec["owner_state"]["code"] == "other_host"
    second.session = rec["token"]
    assert second.json("session", "status", session=False)["lease"]["history"][-1]["operator_confirmed"] == reason
    assert owner.run("note", "late").returncode == 4
    second.json("end", "--summary", "done")
    assert any(reason in n for n in _archive_notes(store))

    # без связи с Windows (в этом же процессе) — тоже unknown, не dead
    assert owner_state(claude_host)["code"] == "host_alive_agent_unobservable"
    monkeypatch.setattr(session_mod, "WINDOWS_POWERSHELL", None)
    assert owner_state(claude_host)["code"] == "no_interop"
