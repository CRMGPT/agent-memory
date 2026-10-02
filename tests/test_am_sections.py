"""Четыре раздела памяти: признак записи внутри одной памяти.

code / logic / data / workflow. Новая запись получает раздел; один факт не копируется ради
другого раздела; поиск и сборка контекста фильтруют по разделам, а без фильтра берут полезные
записи из всех разделов в общем бюджете. Записи прежней версии — «не классифицированы»
(техническое состояние, не пятый раздел), доступны и классифицируются явным UPDATE.
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from agent_memory import Context, Memory
from agent_memory import schema as S

FACTS = {
    "code": ("AuthService rotate_token issues a new token through _issue", "code",
             "code:app/auth/service.py::AuthService.rotate_token"),
    "logic": ("Token rotation must revoke the old token on every refresh", "doc", "docs/auth.md"),
    "data": ("Refresh tokens are stored as redis keys token to user id", "file", "app/auth/service.py"),
    "workflow": ("Run the auth tests with pytest tests/test_refresh_race.py before a token change", "file",
                 "tests/test_refresh_race.py"),
}


def add_all(mem) -> dict[str, str]:
    ids = {}
    for section, (statement, kind, ref) in FACTS.items():
        ids[section] = mem.add({"type": "FACT", "section": section, "statement": statement,
                                "evidence": [{"kind": kind, "ref": ref}]})["added"][0]
    return ids


def test_four_sections_are_stored_and_read_with_sources(mem):
    ids = add_all(mem)
    for section, rid in ids.items():
        rec = mem.get(rid)
        assert rec["section"] == section and rec["statement"] == FACTS[section][0]
        assert mem.sources(rid)[0]["available"]


@pytest.mark.parametrize("record,needle", [
    ({"type": "FACT", "statement": "no section", "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}, "section None"),
    ({"type": "FACT", "section": "misc", "statement": "bad section",
      "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}, "section 'misc'"),
])
def test_a_new_record_needs_a_valid_section(mem, record, needle):
    with pytest.raises(S.ValidationError, match=needle):
        mem.add(record)


def test_episode_summary_is_workflow(ctx):
    ctx.begin("small task")
    res = ctx.end(None, summary="finished the small task")
    rid = res["commit"]["added"][0]
    assert ctx.mem.get(rid)["section"] == "workflow"


def test_filters_and_mixed_query_without_duplicates(mem, ctx):
    ids = add_all(mem)
    only = mem.search("token", limit=20, sections=("logic",))
    assert [h["id"] for h in only] == [ids["logic"]]
    two = {h["id"] for h in mem.search("token", limit=20, sections=("code", "data"))}
    assert two == {ids["code"], ids["data"]}
    pack = ctx.build("rotate_token _issue: revoke old token rule, redis token keys, run pytest auth tests")
    assert set(ids.values()) <= set(pack["injected"])  # все четыре раздела в одном наборе
    assert len(pack["injected"]) == len(set(pack["injected"]))
    assert "· logic ·" in pack["markdown"] and "· workflow ·" in pack["markdown"]


def test_the_same_fact_in_another_section_is_not_copied(mem):
    first = mem.add({"type": "FACT", "section": "logic", "statement": "Rotation is atomic",
                     "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    again = mem.add({"type": "FACT", "section": "code", "statement": "Rotation is atomic",
                     "evidence": [{"kind": "file", "ref": "app/auth/service.py"}]})
    assert again["added"] == [] and again["duplicates_converted"][0]["into"] == first
    assert mem.get(first)["section"] == "logic"
    assert mem.store.k.conn.execute("select count(*) from records where statement='Rotation is atomic'").fetchone()[0] == 1


def test_all_sections_fit_one_budget(mem, ctx):
    ids = add_all(mem)
    for i in range(40):  # много похожих записей одного раздела не вытесняют остальные
        mem.add({"type": "FACT", "section": "code", "coexist": True,
                 "statement": f"token helper number {i} formats the token string variant {i}",
                 "evidence": [{"kind": "file", "ref": "app/auth/service.py"}]})
    pack = ctx.build("token rotation rules storage tests", budget=900)
    assert pack["pack_tokens_est"] <= 900 + 120  # заголовки и рабочее состояние — небольшой запас
    got = {mem.get(r)["section"] for r in pack["injected"]}
    assert {"logic", "data", "workflow"} <= got, got


def test_secrets_are_refused(mem):
    # поддельные значения собираются из частей: в исходниках нет строк в формате настоящих ключей
    for text in ("api_key = " + "sk-" + "ant-api03-abcdefghijklmnopqrstuv", "pass" "word: hunter2hunter2",
                 "gh" + "p_abcdefghijklmnopqrstuvwxyz0123", "-----BEGIN RSA " + "PRIVATE KEY-----"):
        with pytest.raises(S.ValidationError, match="secret"):
            mem.add({"type": "FACT", "section": "workflow", "statement": f"config uses {text}",
                     "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})


def _old_store(repo, tmp_path):
    """Хранилище прежней сборки: записи без раздела (столбца section нет)."""
    store = tmp_path / "old-store"
    mem = Memory(store, repo, roots=("app", "tests"))
    rid = mem.add({"type": "DECISION", "section": "logic", "statement": "Old decision about rotation order",
                   "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    mem.close()
    db = sqlite3.connect(str(store / "memory.sqlite"))
    cols = [r[1] for r in db.execute("pragma table_info(records)") if r[1] != "section"]
    db.executescript(f"""
        create table records_old as select {', '.join(cols)} from records;
        drop table records;
        alter table records_old rename to records;
    """)
    db.close()
    return store, rid


def test_old_records_stay_available_unclassified_and_can_be_classified(repo, tmp_path):
    store, rid = _old_store(repo, tmp_path)
    backup = tmp_path / "backup"
    shutil.copytree(store, backup)
    mem = Memory(store, repo, roots=("app", "tests"))  # открытие добавляет столбец, данные целы
    try:
        rec = mem.get(rid)
        assert rec["statement"] == "Old decision about rotation order" and rec["section"] is None
        hits = mem.search("rotation order")
        assert hits and hits[0]["id"] == rid and hits[0]["section"] == S.UNCLASSIFIED
        assert mem.search("rotation order", sections=("logic",)) == []
        assert mem.search("rotation order", sections=(S.UNCLASSIFIED,))[0]["id"] == rid
        assert rid in Context(mem).build("rotation order")["injected"]  # без фильтра — в наборе
        mem.update(rid, {"section": "logic"}, expected_version=rec["version"], reason="classified")
        assert mem.get(rid)["section"] == "logic" and mem.sources(rid)[0]["available"]
        with pytest.raises(S.ValidationError):
            mem.update(rid, {"section": "misc"}, expected_version=mem.get(rid)["version"])
    finally:
        mem.close()
    again = Memory(store, repo, roots=("app", "tests"))  # повторное открытие ничего не ломает
    try:
        assert again.get(rid)["section"] == "logic"
    finally:
        again.close()
    cols = {r[1] for r in sqlite3.connect(str(backup / "memory.sqlite")).execute("pragma table_info(records)")}
    assert "section" not in cols  # копия до перехода не тронута


def test_section_survives_supersede(mem):
    rid = mem.add({"type": "DECISION", "section": "logic", "subject": "auth.order",
                   "statement": "Delete old token first", "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    rep = mem.commit({"ops": [{"op": "SUPERSEDE", "id": rid, "expected_version": 1, "reason": "changed",
                               "record": {"statement": "Store new token first",
                                          "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}}]})
    new = rep["added"][0]
    assert mem.get(new)["section"] == "logic"
    assert json.dumps(mem.get(new))  # запись сериализуется для CLI
