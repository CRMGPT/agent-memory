"""Регрессии схемы 2: каждый тест воспроизводит найденный сценарий."""

from __future__ import annotations

import json

import pytest

from agent_memory import Context, LeaseError, Memory
from am_helpers import git, write

README_CHECK = {"kind": "check", "method": "pattern", "ref": "README.md", "pattern": ".", "expect": "match",
                "condition": "README is not empty"}


def _fact(statement, evidence):
    return {"type": "FACT", "section": "code", "statement": statement, "evidence": evidence}


def test_uncommitted_knowledge_does_not_leak_after_stash_or_branch_switch(repo, tmp_path):
    """Находка 1: запись на незакоммиченных правках не становится истиной после stash и чужого коммита."""
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, "app/auth/service.py", (repo / "app/auth/service.py").read_text(encoding="utf-8") + "\n# experiment\n")
    mem = Memory(tmp_path / "store", repo, roots=("app",))
    try:
        rid = mem.add(_fact("service has an experiment marker", [{"kind": "file", "ref": "app/auth/service.py"}]))[
            "added"][0]
        assert mem.get(rid)["applicability"] == "here"
        git(repo, "stash", "-q")
        assert mem.get(rid)["applicability"] == "withdrawn" and not mem.get(rid)["applies_here"]
        git(repo, "checkout", "-q", "main")
        write(repo, "docs/unrelated.md", "x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "unrelated work on main")
        assert mem.anchor_pending() == 0  # правок из отпечатка в дереве нет — не привязываем
        assert not mem.get(rid)["applies_here"]
        # Эксперимент вернули и закоммитили — теперь привязка честная.
        git(repo, "checkout", "-q", "feature")
        git(repo, "stash", "pop", "-q")
        assert mem.get(rid)["applicability"] == "here"
        git(repo, "commit", "-qam", "keep experiment")
        assert mem.anchor_pending() == 1
        rec = mem.get(rid)
        assert rec["applicability"] == "history" and rec["observed_commit"] == git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "main")
        assert mem.get(rid)["applicability"] == "other"  # на main ветка не влита
    finally:
        mem.close()


def test_pytest_check_goes_stale_when_code_under_test_changes(repo, tmp_path):
    """Находка 2: изменение кода (не файла теста) и другая рабочая копия — проверка не свежая."""
    write(repo, "app/mathx.py", "def add(a, b):\n    return a + b\n")
    write(repo, "tests/test_mathx.py", "from app.mathx import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "mathx")
    mem = Memory(tmp_path / "store", repo, roots=("app", "tests"))
    try:
        rid = mem.add(_fact("add() sums two integers", [
            {"kind": "check", "method": "pytest", "ref": "tests/test_mathx.py::test_add",
             "condition": "test_add asserts add(2, 2) == 4"}]))["added"][0]
        assert mem.get(rid)["certainty_here"] == "verified"
        write(repo, "app/mathx.py", "def add(a, b):\n    return a - b\n")  # тест теперь падает
        assert mem.get(rid)["certainty_here"] == "needs_recheck"
        write(repo, "app/mathx.py", "def add(a, b):\n    return a + b\n")
        assert mem.get(rid)["certainty_here"] == "verified"  # то же состояние кода — тот же прогон
    finally:
        mem.close()
    wt = tmp_path / "wt2"
    git(repo, "worktree", "add", "-q", "-b", "other", str(wt))
    write(wt, "app/mathx.py", "def add(a, b):\n    return 0\n")
    other = Memory(tmp_path / "store", wt, roots=("app", "tests"))
    try:
        assert other.get(rid)["certainty_here"] == "needs_recheck"  # прогон в копии A — не прогон в копии B
    finally:
        other.close()


def test_trivial_pattern_check_is_not_verified(mem):
    """Находка 3: шаблон «.» на README подтверждает только наличие текста."""
    rid = mem.add(_fact("The service is written in Rust and uses Postgres", [README_CHECK]))["added"][0]
    assert mem.get(rid)["certainty_here"] == "check_passed"
    with pytest.raises(Exception, match="fresh passing pytest"):
        mem.add({**_fact("Rust again", [README_CHECK]), "confidence": 0.99})


def test_ownership_is_rechecked_inside_the_write_transaction(mem, ctx, monkeypatch):
    """Находка 4: аренду забрали, пока шли проверки, — end не закрывает уже чужой эпизод."""
    from agent_memory import checks

    ctx.begin("slow task")
    ctx.note("raw")
    real = checks.prepare_ops

    def stolen(m, ops):
        out = real(m, ops)
        m.store.k.conn.execute("update leases set token='s-thief' where scope=?", (m.scope,))
        m.store.k.conn.execute("update episodes set owner_token='s-thief' where status='open'")
        return out

    monkeypatch.setattr(checks, "prepare_ops", stolen)
    with pytest.raises(LeaseError, match="ownership changed"):
        ctx.end({"ops": [{"op": "ADD", "record": _fact("x", [{"kind": "doc", "ref": "docs/auth.md"}])}]})
    assert mem.store.count_records() == 0 and mem.store.notes("episode_000001")


def test_end_replay_keeps_explicit_diff_id(repo, cli_factory, tmp_path):
    """Находка 5: повтор end с явным diff_id после обрыва — replayed, без дублей."""
    cli = cli_factory(repo, tmp_path / "store")
    cli.start()
    assert cli.run("begin", "t").returncode == 0
    diff = json.dumps({"diff_id": "end-7", "ops": [{"op": "ADD", "record": _fact(
        "docs mention rotation", [{"kind": "doc", "ref": "docs/auth.md"}])}]})
    assert cli.run("end", "--diff", diff, fault="end_after_commit:exit").returncode == 91
    retry = cli.json("end", "--diff", diff)
    assert retry["replayed"] is True
    assert len([h for h in cli.json("search", "docs mention rotation") if h["id"].startswith("fact")]) == 1


def test_working_file_failure_after_save_is_exit_6(repo, cli_factory, tmp_path):
    """Находка 6: сбой файла после записи в базу — «сохранено, нужен repair», а не «ничего не сохранено»."""
    cli = cli_factory(repo, tmp_path / "store")
    cli.start()
    f = tmp_path / "w.md"
    f.write_text("# Goal\nnew goal\n", encoding="utf-8")
    out = cli.run("working", "set", str(f), fault="write_working")
    assert out.returncode == 6 and "database SAVED" in out.stderr
    assert cli.run("working", "show").stdout.startswith("# Goal\nnew goal")


def test_startup_repair_failure_does_not_break_reads(mem, ctx, monkeypatch, tmp_path, repo, cli_factory):
    cli = cli_factory(repo, tmp_path / "store2")
    cli.start()
    assert cli.run("begin", "t").returncode == 0
    assert cli.run("end", fault="export_archive").returncode == 6
    out = cli.run("search", "anything", fault="export_archive")  # архив всё ещё не пишется
    assert out.returncode == 0 and "WARNING" in out.stderr


def test_migration_maps_url_only_v1_records_like_v2():
    """Находка 7: v1-запись только с URL после перехода — attested, как новые записи v2."""
    from agent_memory.migrate import _new_certainty

    assert _new_certainty("verified", {"url"}) == "attested"
    assert _new_certainty("high", {"user"}) == "attested"
    assert _new_certainty("verified", {"code", "url"}) == "sourced"


def test_v1_deterministic_edges_in_shared_db_are_ignored(mem):
    """Код v1, продолжающий писать граф в общую базу, не попадает в выдачу v2."""
    rid = mem.add(_fact("x", [{"kind": "doc", "ref": "docs/auth.md"}]))["added"][0]
    mem.store.k.add_edge(rid, "CALLS", "code:app/ghost.py::ghost", "deterministic")
    assert all(e["rel"] != "CALLS" for e in mem.store.edges_of(rid))


def test_scope_isolation_uses_context_of_own_memory(tmp_path, repo):
    a = Context(Memory(tmp_path / "s", repo, roots=("app",), scope="A"))
    a.start_session()
    assert a.begin("x")["episode"] == "episode_000001"


def test_knowledge_recorded_mid_task_survives_other_edits_and_anchors_on_commit(repo, tmp_path):
    """Правка постороннего файла не отменяет запись; коммит работы её привязывает."""
    mem = Memory(tmp_path / "store", repo, roots=("app",))
    try:
        git(repo, "checkout", "-q", "-b", "task")
        write(repo, "app/auth/service.py", (repo / "app/auth/service.py").read_text(encoding="utf-8") + "\n# a\n")
        write(repo, "app/api/refresh.py", (repo / "app/api/refresh.py").read_text(encoding="utf-8") + "\n# b\n")
        rid = mem.add(_fact("service gained marker a", [{"kind": "file", "ref": "app/auth/service.py"}]))["added"][0]
        old = mem.add(_fact("refresh controller is untouched", [{"kind": "doc", "ref": "docs/auth.md"}]))["added"][0]
        assert mem.get(rid)["applicability"] == "here"
        assert mem.get(old)["applicability"] == "history"  # о файле без правок — сразу на коммите
        write(repo, "app/api/refresh.py", (repo / "app/api/refresh.py").read_text(encoding="utf-8") + "# b2\n")
        assert mem.get(rid)["applicability"] == "here"  # посторонняя правка ничего не меняет
        write(repo, "app/auth/service.py", (repo / "app/auth/service.py").read_text(encoding="utf-8") + "# a2\n")
        # Тот же файл правят дальше: запись была о прежнем содержимом — консервативно withdrawn.
        assert mem.get(rid)["applicability"] == "withdrawn"
        # Замена решения посреди задачи: тоже на правках, тоже должна вступить в силу после коммита.
        mem.commit({"ops": [{"op": "SUPERSEDE", "id": rid, "expected_version": 1, "reason": "refined",
                             "record": {"statement": "service gained markers a and a2",
                                        "evidence": [{"kind": "file", "ref": "app/auth/service.py"}]}}]})
        assert mem.get(rid)["status_here"] == "superseded"
        git(repo, "commit", "-qam", "task work")
        assert mem.anchor_pending() >= 2  # замена и перемена статуса; прежняя запись — о незакоммиченном тексте
        assert mem.get(rid)["applicability"] == "withdrawn" and mem.get(rid)["status_here"] == "superseded"
        new = mem.get("fact_00003")
        assert new["applicability"] == "history" and new["status_here"] == "active"
        git(repo, "checkout", "-q", "main")
        assert mem.get(rid)["status_here"] == "active" and not mem.get("fact_00003")["applies_here"]
        git(repo, "merge", "-q", "--no-edit", "task")
        assert mem.get(rid)["status_here"] == "superseded" and mem.get("fact_00003")["applies_here"]
    finally:
        mem.close()


def test_early_v2_store_gets_late_columns_on_open(tmp_path, repo):
    import sqlite3

    m = Memory(tmp_path / "s", repo, roots=("app",))
    m.close()
    db = sqlite3.connect(tmp_path / "s" / "memory.sqlite")
    for table, col in (("records", "observed_patch"), ("evidence", "check_patch"), ("transitions", "patch")):
        db.execute(f"alter table {table} drop column {col}")  # как база ранней сборки схемы 2
    db.commit()
    db.close()
    m = Memory(tmp_path / "s", repo, roots=("app",))
    try:
        assert m.add(_fact("works after reopen", [{"kind": "doc", "ref": "docs/auth.md"}]))["added"]
    finally:
        m.close()


def test_discarded_experiment_is_not_anchored_by_a_later_commit_to_the_same_file(repo, tmp_path):
    """Правку выбросили, потом другой коммит изменил тот же файл — запись не привязывается."""
    mem = Memory(tmp_path / "store", repo, roots=("app",))
    try:
        svc = repo / "app/auth/service.py"
        original = svc.read_text(encoding="utf-8")
        write(repo, "app/auth/service.py", original + "\n# experiment: return 3\n")
        rid = mem.add(_fact("service returns three", [{"kind": "file", "ref": "app/auth/service.py"}]))["added"][0]
        assert mem.get(rid)["applicability"] == "here"
        git(repo, "checkout", "--", "app/auth/service.py")
        assert mem.get(rid)["applicability"] == "withdrawn"
        write(repo, "app/auth/service.py", original + "\n\ndef g():\n    return 1\n")
        git(repo, "commit", "-qam", "unrelated change to the same file")
        assert mem.anchor_pending() == 0
        assert mem.get(rid)["applicability"] == "withdrawn" and not mem.get(rid)["applies_here"]
    finally:
        mem.close()
    other = Memory(tmp_path / "store", repo, roots=("app",), scope="another-worktree")
    try:
        assert not other.get(rid)["applies_here"]  # для других копий выброшенный эксперимент не знание
    finally:
        other.close()
