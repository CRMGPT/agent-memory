"""Граница доверия к сведениям о владельце.

dead — только по ПОЛНОМУ ответу в рамке AMV1 BEGIN / AMV1 END с тем же непустым именем
компьютера и явной строкой PROC про записанный pid: ABSENT (процесса нет) или PRESENT с другим
временем создания (pid занят). Неполный, пустой, некорректный или противоречивый ответ,
ошибка и таймаут — unknown. Неполная запись владельца (пустое имя компьютера, нет pid или
времени) — unknown без запроса к Windows.

Почти все проверки здесь — ПОДМЕНА ответа PowerShell (monkeypatch `_powershell`), а не
настоящий сбой ОС; настоящие запросы к Windows — в test_am_owner_lifecycle.py (claude_host).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

import agent_memory.session as session_mod
from agent_memory import Context
from agent_memory.schema import LeaseError
from agent_memory.session import capture_owner, host_status, owner_state

REC = {"kind": "claude-host", "name": "claude.exe", "pid": 1234, "created": "100", "computer": "PC"}


def framed(*lines: str) -> str:
    return "AMV1 BEGIN\n" + "".join(line + "\n" for line in lines) + "AMV1 END\n"


def answer(monkeypatch, text: str, status: str = "ok") -> list[str]:
    calls: list[str] = []

    def fake(script: str):
        calls.append(script)
        return status, text

    monkeypatch.setattr(session_mod, "_powershell", fake)
    return calls


# Ответы старого формата, без рамки AMV1: раньше три последних ошибочно давали dead.
OUTPUT_EXAMPLES = [
    "HOST\tPC\nP\tclaude.exe\t100\n",
    "HOST\tPC\nP\tclaude.exe\t\n",
    "HOST\tPC\nP\tclaude.exe\tinvalid\n",
    "HOST\tPC\nP\tclaude.exe\n",
]


@pytest.mark.parametrize("text", OUTPUT_EXAMPLES)
def test_unframed_examples_never_give_dead(monkeypatch, text):
    answer(monkeypatch, text)
    assert owner_state(REC)["state"] == "unknown"


COMPLETE = [
    (framed("HOST\tPC", "PROC\tPRESENT\t1234\tclaude.exe\t100"), "unknown", "host_alive_agent_unobservable"),
    (framed("HOST\tPC", "PROC\tABSENT\t1234"), "dead", "host_gone"),
    (framed("HOST\tPC", "PROC\tPRESENT\t1234\tnotepad.exe\t555"), "dead", "host_reused"),
    (framed("HOST\tOTHER", "PROC\tABSENT\t1234"), "unknown", "other_host"),
    (framed("HOST\tPC\r", "PROC\tABSENT\t1234\r"), "dead", "host_gone"),  # CRLF Windows
]

INCOMPLETE = {
    "no frame at all": "HOST\tPC\nPROC\tABSENT\t1234\n",
    "no end marker": "AMV1 BEGIN\nHOST\tPC\nPROC\tABSENT\t1234\n",
    "no begin marker": "HOST\tPC\nPROC\tABSENT\t1234\nAMV1 END\n",
    "empty answer": "",
    "frame only": framed(),
    "no PROC line": framed("HOST\tPC"),
    "no HOST line": framed("PROC\tABSENT\t1234"),
    "empty computer name": framed("HOST\t", "PROC\tABSENT\t1234"),
    "blank computer name": framed("HOST\t  ", "PROC\tABSENT\t1234"),
    "PRESENT without time": framed("HOST\tPC", "PROC\tPRESENT\t1234\tclaude.exe\t"),
    "PRESENT invalid time": framed("HOST\tPC", "PROC\tPRESENT\t1234\tclaude.exe\tinvalid"),
    "PRESENT zero time": framed("HOST\tPC", "PROC\tPRESENT\t1234\tclaude.exe\t0"),
    "PRESENT short line": framed("HOST\tPC", "PROC\tPRESENT\t1234\tclaude.exe"),
    "PRESENT empty name": framed("HOST\tPC", "PROC\tPRESENT\t1234\t\t555"),
    "PRESENT other pid": framed("HOST\tPC", "PROC\tPRESENT\t999\tclaude.exe\t555"),
    "ABSENT other pid": framed("HOST\tPC", "PROC\tABSENT\t999"),
    "ABSENT without pid": framed("HOST\tPC", "PROC\tABSENT"),
    "two PROC lines": framed("HOST\tPC", "PROC\tABSENT\t1234", "PROC\tPRESENT\t1234\tclaude.exe\t100"),
    "two HOST lines": framed("HOST\tPC", "HOST\tPC", "PROC\tABSENT\t1234"),
    "unknown status word": framed("HOST\tPC", "PROC\tMAYBE\t1234"),
    "extra line": framed("HOST\tPC", "PROC\tABSENT\t1234", "NOTE\tx"),
    "nested frame": framed("HOST\tPC", "AMV1 END", "PROC\tABSENT\t1234"),
}


@pytest.mark.parametrize("text,state,code", COMPLETE)
def test_complete_answers(monkeypatch, text, state, code):
    answer(monkeypatch, text)
    got = owner_state(REC)
    assert (got["state"], got["code"]) == (state, code)


@pytest.mark.parametrize("name", sorted(INCOMPLETE))
def test_incomplete_answers_are_unknown(monkeypatch, name):
    answer(monkeypatch, INCOMPLETE[name])
    got = owner_state(REC)
    assert got["state"] == "unknown" and got["code"] == "query_failed", (name, got)


@pytest.mark.parametrize("status", ["query_failed", "no_interop"])
def test_errors_are_unknown(monkeypatch, status):
    answer(monkeypatch, "boom", status=status)
    got = owner_state(REC)
    assert got["state"] == "unknown" and got["code"] == status


def test_timeout_and_nonzero_exit_are_unknown(monkeypatch, tmp_path):
    exe = tmp_path / "powershell.exe"
    exe.write_text("")
    monkeypatch.setattr(session_mod, "WINDOWS_POWERSHELL", str(exe))

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=40)

    monkeypatch.setattr(session_mod.subprocess, "run", timeout)
    assert owner_state(REC)["code"] == "query_failed"
    monkeypatch.setattr(session_mod.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 1, b"", b"CIM failed"))
    got = owner_state(REC)
    assert got["state"] == "unknown" and got["code"] == "query_failed"


@pytest.mark.parametrize("field,value", [("computer", ""), ("computer", "  "), ("computer", None),
                                         ("pid", "abc"), ("pid", 0), ("pid", None), ("created", ""),
                                         ("created", "invalid"), ("created", "0")])
def test_incomplete_owner_record_is_unknown_without_asking_windows(monkeypatch, field, value):
    calls = answer(monkeypatch, framed("HOST\tPC", "PROC\tABSENT\t1234"))
    got = owner_state({**REC, field: value})
    assert got["state"] == "unknown" and got["code"] == "incomplete_owner_record"
    assert calls == []  # пустое имя компьютера не отключает сравнение — запрос даже не делается


CHAIN = ["P\t5\t4\tpowershell.exe\t50", "P\t4\t3\tbash.exe\t40", "P\t3\t2\tclaude.exe\t30",
         "P\t2\t1\tclaude.exe\t20", "P\t1\t0\tsihost.exe\t10"]


def test_capture_requires_a_complete_answer(monkeypatch):
    answer(monkeypatch, framed("SELF\t5\tPC", *CHAIN))
    got = capture_owner()
    assert got["kind"] == "claude-host" and got["pid"] == 3 and got["computer"] == "PC"
    answer(monkeypatch, framed("SELF\t5\t", *CHAIN))
    got = capture_owner()
    assert got["kind"] == "uncaptured" and got["code"] == "incomplete_owner_record"
    for bad in (framed("SELF\t5\tPC", *CHAIN, "P\t9\t8\tx.exe"),       # неполная строка процесса
                framed("SELF\tx\tPC", *CHAIN),                          # некорректный pid
                framed(*CHAIN),                                         # нет SELF
                "SELF\t5\tPC\n" + "\n".join(CHAIN)):                    # нет рамки
        answer(monkeypatch, bad)
        got = capture_owner()
        assert got["kind"] == "uncaptured" and got["code"] == "query_failed", bad
    assert owner_state(capture_owner())["state"] == "unknown"


def test_host_status_matches_owner_state_for_alive(monkeypatch):
    answer(monkeypatch, framed("HOST\tPC", "PROC\tPRESENT\t1234\tclaude.exe\t100"))
    assert host_status(REC)["state"] == "alive"


# ================================================= recover: отказ и разрешённое восстановление


def _lease_and_episode(store) -> tuple:
    db = sqlite3.connect(str(store / "memory.sqlite"))
    lease = db.execute("select token, owner_proc, history from leases").fetchone()
    ep = db.execute("select id, status, owner_token from episodes").fetchall()
    notes = db.execute("select episode_id, seq, kind, text from episode_notes order by seq").fetchall()
    db.close()
    return lease, ep, notes


@pytest.mark.parametrize("record", [{**REC, "computer": ""}, {**REC, "created": "bad"}, REC])
def test_refused_recover_changes_nothing_and_the_owner_continues(repo, cli_factory, tmp_path, record):
    """Неполная запись, а для полной — отсутствие связи с Windows (conftest): unknown, отказ."""
    store = tmp_path / "store"
    owner = cli_factory(repo, store)
    owner.start()
    assert owner.run("begin", "owner task").returncode == 0
    owner.json("note", "owner note")
    db = sqlite3.connect(str(store / "memory.sqlite"))
    db.execute("update leases set owner_proc=?", (json.dumps(record),))
    db.commit()
    db.close()
    before = _lease_and_episode(store)
    second = cli_factory(repo, store)
    out = second.run("session", "recover", session=False)
    assert out.returncode == 4 and "unknown" in out.stderr
    assert _lease_and_episode(store) == before  # токен, история, заметки и открытый эпизод — те же
    assert owner.json("note", "owner continues")["seq"] == 2


def test_recover_after_a_complete_absent_answer_keeps_notes_and_rejects_the_old_token(mem, monkeypatch):
    owner = Context(mem)
    owner.start_session("owner")
    owner.begin("owner task")
    owner.note("owner note")
    with mem.store.transaction():
        mem.store.k.conn.execute("update leases set owner_proc=?", (json.dumps(REC),))
    second = Context(mem)
    answer(monkeypatch, framed("HOST\tPC", "PROC\tPRESENT\t1234\tclaude.exe\t100", "junk"))
    with pytest.raises(LeaseError):  # неполный ответ: отказ
        second.leases.recover("second")
    answer(monkeypatch, framed("HOST\tPC", "PROC\tABSENT\t1234"))
    monkeypatch.setattr(session_mod, "capture_owner", lambda: {"kind": "uncaptured", "code": "no_interop"})
    rec = second.leases.recover("second")
    assert rec["owner_state"]["code"] == "host_gone" and rec["adopted_episode"]
    second.session = rec["token"]
    texts = [n["text"] for n in mem.store.notes(rec["adopted_episode"])]
    assert texts[0] == "owner note" and "took over" in texts[1]
    with pytest.raises(LeaseError):
        owner.note("late write")
    assert second.end(None, summary="finished after recovery")["saved"]


def test_capture_is_never_trusted_from_the_environment(monkeypatch):
    """CLAUDE_PID и подобные переменные не используются: владелец — только по ответу Windows."""
    monkeypatch.setenv("CLAUDE_PID", "1234")
    answer(monkeypatch, "", status="no_interop")
    assert capture_owner()["kind"] == "uncaptured"
