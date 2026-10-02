"""end сам приводит карту кода своей рабочей копии к файлам на диске.

Раньше begin строил карту, исполнитель правил файлы, а end проверял ссылки по старой карте:
новая функция «не найдена» до ручного `index`. Теперь end перед
проверкой ссылок обновляет карту своей копии инкрементально; без изменений — не трогает её.
Обновление не повышает достоверность; ошибка не закрывает эпизод; параллельная правка — отказ.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import agent_memory.context as context_mod
from agent_memory import Context, Memory
from agent_memory import schema as S

ROTATE = "code:app/auth/service.py::AuthService.rotate_token"


def fact(statement: str, ref: str, kind: str = "code") -> dict:
    return {"ops": [{"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": statement,
                                            "evidence": [{"kind": kind, "ref": ref}]}}]}


def records_with(mem, statement: str) -> list[dict]:
    rows = mem.store.k.conn.execute("select id from records where statement=?", (statement,)).fetchall()
    return [dict(r) for r in rows]


def index_runs(mem) -> int:
    return sum(1 for m in mem.store.metrics() if m["event"] == "index")


def test_new_untracked_file_is_found_by_end_without_manual_index(mem, ctx, put):
    ctx.begin("add a helper")
    put("app/auth/helpers.py", "def fresh_helper():\n    return 7\n")
    res = ctx.end(fact("fresh_helper returns 7", "fresh_helper"))
    assert res["saved"] and res["code_map"]["reindexed"]
    rid = res["commit"]["added"][0]
    src = mem.sources(rid)[0]
    assert src["available"] and src["verification"] in ("unchanged", "present")
    assert mem.get(rid)["certainty"] == "sourced"  # обновление карты не повышает достоверность


def test_modified_function_marks_old_knowledge_stale_and_new_knowledge_is_current(mem, ctx, put, auth_source):
    old = mem.add({"type": "DECISION", "section": "logic", "statement": "Rotation deletes the old token first",
                   "evidence": [{"kind": "code", "ref": ROTATE}]})["added"][0]
    ctx.begin("change rotation")
    put("app/auth/service.py", auth_source.replace("self.client.delete(old_token)\n",
                                                   "self.client.delete(old_token)\n        self.audit = True\n"))
    res = ctx.end(fact("rotate_token now sets audit", ROTATE))
    assert res["saved"] and res["code_map"]["reindexed"] and res["code_map"]["stale_records"] >= 1
    assert mem.get(old)["stale_reason"]  # старое знание о прежнем теле — устарело, а не «подтверждено»
    new = res["commit"]["added"][0]
    assert mem.sources(new)[0]["verification"] == "unchanged"


def test_deleted_file_is_not_treated_as_present(mem, ctx, repo):
    old = mem.add({"type": "FACT", "section": "code", "statement": "refresh calls rotate_token",
                   "evidence": [{"kind": "code", "ref": "code:app/api/refresh.py::refresh"}]})["added"][0]
    ctx.begin("remove refresh endpoint")
    (repo / "app/api/refresh.py").unlink()
    res = ctx.end(None, summary="endpoint removed")
    assert res["saved"] and res["code_map"]["reindexed"] and res["code_map"]["missing"] >= 1
    assert mem.get(old)["stale_reason"]
    assert mem.sources(old)[0]["verification"] == "missing"
    ctx.begin("next")
    with pytest.raises(S.MemoryError_):  # ссылка на исчезнувший код не принимается
        ctx.end(fact("refresh still exists", "refresh"))
    assert ctx.current() is not None


def test_renamed_file_keeps_knowledge_attached(mem, ctx, repo):
    old = mem.add({"type": "FACT", "section": "code", "statement": "refresh delegates to the service",
                   "evidence": [{"kind": "code", "ref": "code:app/api/refresh.py::refresh"}]})["added"][0]
    ctx.begin("rename module")
    (repo / "app/api/refresh.py").rename(repo / "app/api/renew.py")
    res = ctx.end(fact("renew module holds refresh", "code:app/api/renew.py::refresh"))
    assert res["saved"] and res["code_map"]["moved"] == 1
    assert mem.get(old)["stale_reason"] is None
    assert mem.sources(old)[0]["current_ref"] == "code:app/api/renew.py::refresh"


def test_end_without_changes_does_not_reindex(mem, ctx):
    ctx.begin("read only")
    runs = index_runs(mem)
    res = ctx.end(fact("rotate_token exists", ROTATE))
    assert res["code_map"] == {"reindexed": False} and index_runs(mem) == runs


def test_failed_update_keeps_the_episode_open_and_retry_saves_once(mem, ctx, put, monkeypatch):
    ctx.begin("with failure")
    ctx.note("work note")
    put("app/auth/helpers.py", "def flaky_helper():\n    return 1\n")
    monkeypatch.setenv("AGENT_MEMORY_FAULT", "end_reindex")
    with pytest.raises(S.MemoryError_) as err:
        ctx.end(fact("flaky_helper returns 1", "flaky_helper"))
    assert "nothing saved" in str(err.value) and "retry" in str(err.value)
    ep = ctx.current()
    assert ep is not None and [n["text"] for n in mem.store.notes(ep["id"])] == ["work note"]
    assert records_with(mem, "flaky_helper returns 1") == []
    monkeypatch.delenv("AGENT_MEMORY_FAULT")
    res = ctx.end(fact("flaky_helper returns 1", "flaky_helper"))
    assert res["saved"] and len(records_with(mem, "flaky_helper returns 1")) == 1
    again = ctx.end(fact("flaky_helper returns 1", "flaky_helper"))  # повтор после успеха — без дублей
    assert again.get("replayed") and len(records_with(mem, "flaky_helper returns 1")) == 1


def test_parallel_edit_during_end_is_refused_without_mixed_state(mem, ctx, put, monkeypatch):
    ctx.begin("racing edit")
    put("app/auth/helpers.py", "def racing_helper():\n    return 1\n")
    real = context_mod.checks.prepare_ops
    calls = []

    def edit_during_checks(m, ops):
        calls.append(1)
        if len(calls) == 1:  # кто-то правит файл, пока end выполняет проверки
            put("app/auth/helpers.py", "def racing_helper():\n    return 2\n")
        return real(m, ops)

    monkeypatch.setattr(context_mod.checks, "prepare_ops", edit_during_checks)
    with pytest.raises(S.ConflictError) as err:
        ctx.end(fact("racing_helper returns 1", "racing_helper"))
    assert "app/auth/helpers.py" in err.value.conflicting
    assert ctx.current() is not None and records_with(mem, "racing_helper returns 1") == []
    res = ctx.end(fact("racing_helper returns 2", "racing_helper"))
    assert res["saved"] and res["code_map"]["reindexed"]


def test_unparseable_referenced_file_refuses_end(mem, ctx, put):
    ctx.begin("half edit")
    put("app/auth/helpers.py", "def broken(:\n")
    with pytest.raises(S.ValidationError) as err:
        ctx.end(fact("helpers module is broken", "app/auth/helpers.py", kind="file"))
    assert "does not parse" in str(err.value) and ctx.current() is not None


def test_two_worktrees_use_their_own_code_map(two_worktrees, tmp_path):
    main, wt_b = two_worktrees
    store = tmp_path / "shared"
    mem_a, mem_b = Memory(store, main, roots=("app", "tests")), Memory(store, wt_b, roots=("app", "tests"))
    try:
        mem_a.index()
        mem_b.index()
        ctx_a, ctx_b = Context(mem_a), Context(mem_b)
        ctx_a.start_session("a")
        ctx_b.start_session("b")
        (wt_b / "app/auth/only_b.py").write_text("def only_in_b():\n    return 'b'\n", encoding="utf-8")
        ctx_b.begin("b adds a helper")
        assert ctx_b.end(fact("only_in_b returns b", "only_in_b"))["saved"]
        ctx_a.begin("a refers to b's helper")
        with pytest.raises(S.MemoryError_):  # свежая карта другой копии — не доказательство для A
            ctx_a.end(fact("only_in_b exists in main", "only_in_b"))
        (main / "app/auth/only_a.py").write_text("def only_in_a():\n    return 'a'\n", encoding="utf-8")
        res = ctx_a.end(fact("only_in_a returns a", "only_in_a"))
        assert res["saved"] and res["code_map"]["reindexed"]
        assert mem_b.store.get_node("code:app/auth/only_a.py::only_in_a") is None
        assert not Path(wt_b / "app/auth/only_a.py").exists()
    finally:
        mem_a.close()
        mem_b.close()


def test_fault_point_is_registered():
    from agent_memory import faults

    assert "end_reindex" in faults.POINTS and os.environ.get("AGENT_MEMORY_FAULT") is None
