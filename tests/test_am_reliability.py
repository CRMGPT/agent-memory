"""Надёжность: новые файлы, свежесть проверок, жизнь владельца аренды.

A. Знание о НОВОМ (ещё не закоммиченном) файле не действует в другой рабочей копии,
   пока это содержимое не попало в её историю.
B. Проверка pytest не остаётся verified, если тест изменился или исчез, если изменился
   новый исходный файл, или если код менялся во время самой проверки.
Жизнь владельца аренды проверяет test_am_owner_lifecycle.py.

Каждый сценарий — настоящие отдельные процессы и короткие интервалы.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_memory import Memory
from am_helpers import git, write

NEW_FILE = "app/auth/new_helper.py"


def _fact_about(path: str, statement: str) -> str:
    return json.dumps({"ops": [{"op": "ADD", "record": {
        "type": "FACT", "section": "code", "statement": statement, "evidence": [{"kind": "file", "ref": path}]}}]})


@pytest.fixture
def same_base(repo: Path, tmp_path: Path):
    """Две рабочие копии на ОДНОМ исходном коммите: A — main, B — ветка side без своих коммитов."""
    wt_b = tmp_path / "wt-b"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt_b))
    assert git(repo, "rev-parse", "HEAD") == git(wt_b, "rev-parse", "HEAD")
    return repo, wt_b


# ============================================================ A. новые файлы


def test_new_file_knowledge_is_local_until_committed_and_merged(same_base, cli_factory, tmp_path):
    main, wt_b = same_base
    store = tmp_path / "shared-store"
    a, b = cli_factory(main, store), cli_factory(wt_b, store)
    a.start()
    b.start()
    write(main, NEW_FILE, "def helper():\n    return 42\n")  # новый файл только у A
    write(main, "notes/draft.txt", "unrelated untracked draft\n")  # посторонний черновик
    rid = a.json("commit", _fact_about(NEW_FILE, "new_helper.helper returns 42"))["added"][0]

    assert a.json("get", rid)["applicability"] == "here"
    seen_by_b = b.json("get", rid)
    assert seen_by_b["applicability"] == "pending" and not seen_by_b["applies_here"]

    # A сохраняет именно это содержимое в коммит; черновик остаётся неотслеживаемым и не мешает.
    # После коммита A в память больше не пишет: применимость не зависит от привязки в A.
    git(main, "add", NEW_FILE)
    git(main, "commit", "-q", "-m", "add helper")
    assert a.json("get", rid)["applies_here"]
    assert b.json("get", rid)["applicability"] == "pending"  # у B этого коммита ещё нет

    git(wt_b, "merge", "-q", "--ff-only", "main")
    assert b.json("get", rid)["applicability"] == "history"

    # Позже A пишет в память — запись привязывается к коммиту; у B ничего не меняется.
    assert a.run("begin", "next task").returncode == 0
    rec_a = a.json("get", rid)
    assert rec_a["applicability"] == "history" and rec_a["observed_commit"] == git(main, "rev-parse", "HEAD")
    assert b.json("get", rid)["applicability"] == "history"

    # Другая запись A на уже закоммиченном файле сразу стоит на коммите: черновик её не держит.
    other = a.json("commit", _fact_about("README.md", "README names the demo"))["added"][0]
    assert a.json("get", other)["applicability"] == "history" and b.json("get", other)["applies_here"]


def test_discarded_new_file_is_never_anchored_by_an_unrelated_commit(same_base, cli_factory, tmp_path):
    main, wt_b = same_base
    store = tmp_path / "shared-store"
    a, b = cli_factory(main, store), cli_factory(wt_b, store)
    a.start()
    b.start()
    write(main, NEW_FILE, "def helper():\n    return 1\n")
    rid = a.json("commit", _fact_about(NEW_FILE, "experimental helper returns 1"))["added"][0]
    assert a.json("get", rid)["applicability"] == "here"

    (main / NEW_FILE).unlink()  # эксперимент выбросили
    assert a.json("get", rid)["applicability"] == "withdrawn"
    write(main, "README.md", "# Demo\n\nunrelated change\n")
    git(main, "commit", "-qam", "unrelated work")
    assert a.run("begin", "after unrelated commit").returncode == 0
    rec = a.json("get", rid)
    assert rec["applicability"] == "withdrawn" and not rec["applies_here"]
    git(wt_b, "merge", "-q", "--ff-only", "main")
    assert not b.json("get", rid)["applies_here"]

    # Тот же файл появился снова, но с другим содержимым и закоммичен — запись не привязывается.
    write(main, NEW_FILE, "def helper():\n    return 2\n")
    git(main, "add", NEW_FILE)
    git(main, "commit", "-q", "-m", "different helper")
    a.run("note", "x")  # любая пишущая команда пытается привязать
    assert a.json("get", rid)["applicability"] == "withdrawn"


def test_new_file_fact_applies_on_main_after_end_commit_merge_without_further_writes(same_base, cli_factory,
                                                                                      tmp_path):
    """Естественный порядок: begin → новый файл → end с FACT → коммит → слияние в main, и всё.
    Исходная копия после коммита в память не пишет — знание всё равно действует на main.
    Так же — перемена статуса (SUPERSEDE) на незакоммиченной правке того же файла."""
    main, wt_b = same_base
    store = tmp_path / "shared-store"
    a, b = cli_factory(main, store), cli_factory(wt_b, store)
    a.start()
    b.start()
    assert b.run("begin", "add helper").returncode == 0
    write(wt_b, NEW_FILE, "def helper():\n    return 42\n")
    end = b.json("end", "--diff", _fact_about(NEW_FILE, "new_helper.helper returns 42"), "--summary", "helper added")
    rid = end["commit"]["added"][0]
    git(wt_b, "add", NEW_FILE)
    git(wt_b, "commit", "-q", "-m", "add helper")
    assert a.json("get", rid)["applicability"] == "pending"  # main ещё без слияния
    git(main, "merge", "-q", "--no-edit", "side")
    seen = a.json("get", rid)
    assert seen["applicability"] == "history" and seen["applies_here"] and seen["status_here"] == "active"

    # Вторая итерация в B: правка того же файла и SUPERSEDE до коммита.
    assert b.run("begin", "change helper").returncode == 0
    write(wt_b, NEW_FILE, "def helper():\n    return 43\n")
    version = b.json("get", rid)["version"]
    replace = {"ops": [{"op": "SUPERSEDE", "id": rid, "expected_version": version, "reason": "value changed",
                        "record": {"type": "FACT", "section": "code", "statement": "new_helper.helper returns 43",
                                   "evidence": [{"kind": "file", "ref": NEW_FILE}]}}]}
    new_id = b.json("end", "--diff", json.dumps(replace), "--summary", "helper changed")["commit"]["added"][0]
    assert a.json("get", rid)["status_here"] == "active" and not a.json("get", new_id)["applies_here"]
    git(wt_b, "commit", "-qam", "helper returns 43")
    git(main, "merge", "-q", "--no-edit", "side")
    assert a.json("get", rid)["status_here"] == "superseded"
    assert a.json("get", new_id)["applies_here"]

    # Выброшенная правка с посторонним коммитом в тот же файл на main не применяется.
    assert b.run("begin", "experiment").returncode == 0
    write(wt_b, NEW_FILE, "def helper():\n    return 99\n")
    exp = b.json("end", "--diff", _fact_about(NEW_FILE, "experimental helper returns 99"))["commit"]["added"][0]
    write(wt_b, NEW_FILE, "def helper():\n    return 44\n")  # другое содержимое
    git(wt_b, "commit", "-qam", "unrelated change of the same file")
    git(main, "merge", "-q", "--no-edit", "side")
    assert a.json("get", exp)["applicability"] == "pending" and not a.json("get", exp)["applies_here"]


def test_git_ignored_files_are_refused_as_sources_and_never_hashed(repo, tmp_path):
    write(repo, ".gitignore", ".env\napp/local_secret.py\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore secrets")
    write(repo, ".env", "TOKEN=abc\n")
    write(repo, "app/local_secret.py", "def secret():\n    return 'abc'\n")
    mem = Memory(tmp_path / "store", repo, roots=("app", "tests"))
    try:
        mem.index()
        assert not mem.store.get_node("code:app/local_secret.py::secret")  # индекс не читает игнорируемое
        for ev in ({"kind": "file", "ref": ".env"}, {"kind": "doc", "ref": ".env", "locator": "L1-L1"},
                   {"kind": "check", "method": "pattern", "ref": ".env", "pattern": "TOKEN", "expect": "match",
                    "condition": "token is set"},
                   {"kind": "check", "method": "pytest", "ref": "app/local_secret.py::test_x",
                    "condition": "x"}):
            with pytest.raises(Exception, match="ignored by git"):
                mem.add({"type": "FACT", "section": "code", "statement": f"secret config via {ev['kind']}", "evidence": [ev]})
        with pytest.raises(Exception, match="ignored by git"):  # связь с игнорируемым файлом
            mem.add({"type": "FACT", "section": "code", "statement": "readme links the env file",
                     "evidence": [{"kind": "file", "ref": "README.md"}],
                     "links": [{"rel": "AFFECTS", "to": "file:.env"}]})
        assert mem.store.k.conn.execute("select count(*) from records").fetchone()[0] == 0
        dump = "\n".join(str(tuple(r)) for t in ("records", "evidence") for r in
                         mem.store.k.conn.execute(f"select * from {t}"))
        assert ".env" not in dump
    finally:
        mem.close()


# ====================================================== B. свежесть проверок

NEW_SOURCE = "app/feature_new.py"
NEW_TEST = "tests/test_feature_new.py"
NEW_TEST_TEXT = ("from app.feature_new import value\n\n\n"
                 "def test_value():\n    assert value() == 1\n")


def _check_fact(ref: str) -> dict:
    return {"type": "FACT", "section": "code", "statement": "feature_new.value returns 1",
            "evidence": [{"kind": "check", "method": "pytest", "ref": ref,
                          "condition": "test_value asserts value() == 1"}]}


def test_new_test_check_loses_verified_when_test_or_new_source_changes(repo, tmp_path):
    write(repo, NEW_SOURCE, "def value():\n    return 1\n")
    write(repo, NEW_TEST, NEW_TEST_TEXT)  # и тест, и код — новые, неотслеживаемые
    mem = Memory(tmp_path / "store", repo, roots=("app", "tests"))
    try:
        rid = mem.add(_check_fact(f"{NEW_TEST}::test_value"))["added"][0]
        assert mem.get(rid)["certainty_here"] == "verified"
        src = [s for s in mem.sources(rid) if s["kind"] == "check"][0]
        # Итог источника не совпадает по имени с уровнем: pytest-проверка — уровень verified.
        assert src["verification"] == "check_ok" and src["check"]["level"] == "verified"

        write(repo, NEW_TEST, NEW_TEST_TEXT + "\n\ndef test_more():\n    assert True\n")  # тест изменили
        got = mem.get(rid)
        assert got["certainty_here"] == "needs_recheck"
        src = [s for s in mem.sources(rid) if s["kind"] == "check"][0]
        assert src["check"]["target"] == "changed" and not src["check"]["fresh"]
        assert src["verification"] == "check_stale"

        write(repo, NEW_TEST, NEW_TEST_TEXT)  # то же содержимое — тот же прогон снова свежий
        assert mem.get(rid)["certainty_here"] == "verified"

        (repo / NEW_TEST).unlink()  # тест исчез
        assert mem.get(rid)["certainty_here"] == "needs_recheck"
        src = [s for s in mem.sources(rid) if s["kind"] == "check"][0]
        assert src["check"]["target"] == "missing" and src["check"]["level"] == "needs_recheck"

        write(repo, NEW_TEST, NEW_TEST_TEXT)
        assert mem.get(rid)["certainty_here"] == "verified"
        write(repo, NEW_SOURCE, "def value():\n    return 1  # edited\n")  # новый исходный файл изменён
        assert mem.get(rid)["certainty_here"] == "needs_recheck"

        # Поиск и ранжирование используют то же исправленное состояние.
        hit = [h for h in mem.search("feature_new value returns") if h["id"] == rid][0]
        assert hit["certainty"] == "needs_recheck"
    finally:
        mem.close()


MUTATING_TEST = f'''
from pathlib import Path


def test_mutates():
    p = Path(__file__).resolve().parents[1] / "{NEW_SOURCE}"
    p.write_text(p.read_text() + "# touched during the check\\n")
    assert True
'''


def test_code_changed_during_the_check_is_not_verified(repo, tmp_path):
    write(repo, NEW_SOURCE, "def value():\n    return 1\n")
    write(repo, "tests/test_mutating.py", MUTATING_TEST)
    mem = Memory(tmp_path / "store", repo, roots=("app", "tests"))
    try:
        with pytest.raises(Exception, match="inconclusive"):
            mem.add(_check_fact("tests/test_mutating.py::test_mutates"))
        assert not mem.search("feature_new value returns")
    finally:
        mem.close()


def test_pattern_source_is_check_ok_but_level_check_passed(mem):
    rid = mem.add({"type": "FACT", "section": "code", "statement": "README mentions the demo", "evidence": [
        {"kind": "check", "method": "pattern", "ref": "README.md", "pattern": "Demo", "expect": "match",
         "condition": "README contains the word Demo"}]})["added"][0]
    src = mem.sources(rid)[0]
    assert src["verification"] == "check_ok" and src["check"]["level"] == "check_passed"
    assert mem.get(rid)["certainty_here"] == "check_passed"


def test_session_token_is_accepted_after_the_subcommand(repo, cli_factory, tmp_path):
    cli = cli_factory(repo, tmp_path / "store")
    token = cli.start()
    assert cli.run("begin", "token placement", "--session", token, session=False).returncode == 0
    assert cli.json("note", "after", "--session", token, session=False)["seq"] == 1
    assert cli.json("--session", token, "note", "before", session=False)["seq"] == 2
    assert cli.json("note", "env")["seq"] == 3  # AGENT_MEMORY_SESSION по-прежнему работает
    out = cli.run("note", "nope", "--session", "s-wrong", session=False)
    assert out.returncode == 4 and "does not own" in out.stderr
    assert cli.run("session", "status", "--session", token, session=False).returncode == 0
