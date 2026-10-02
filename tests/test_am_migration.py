"""Переход хранилища v1 -> v2 на КОПИИ настоящего хранилища, созданного кодом схемы версии 1.

В эталоне (fixtures/v1_store): 7 записей, решение и факт с меткой verified за одно наличие
источника, high от слова владельца, заменённый факт, опровергнутая гипотеза, два закрытых
эпизода с архивами и ОТКРЫТЫЙ эпизод с двумя заметками.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from agent_memory import Context, Memory, MigrationRequired

FIXTURE = Path(__file__).parent / "fixtures" / "v1_store"


@pytest.fixture
def v1(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    shutil.copy2(FIXTURE / "memory.sqlite", store / "memory.sqlite")
    shutil.copytree(FIXTURE / "episodes", store / "episodes")
    shutil.copytree(FIXTURE / "working", store / "working")
    from am_helpers import git, write

    repo = tmp_path / "repo"
    write(repo, "app/__init__.py", "")
    write(repo, "app/auth/__init__.py", "")
    write(repo, "app/auth/service.py", (FIXTURE / "service.py.txt").read_text(encoding="utf-8"))
    write(repo, "docs/auth.md", (FIXTURE / "auth.md.txt").read_text(encoding="utf-8"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return store, repo


def _q(store, sql):
    db = sqlite3.connect(store / "memory.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_new_code_refuses_old_store_without_owner_step(v1, cli_factory):
    store, repo = v1
    with pytest.raises(MigrationRequired):
        Memory(store, repo, roots=("app",))
    out = cli_factory(repo, store, roots="app").run("status", session=False)
    assert out.returncode == 5 and "MIGRATION REQUIRED" in out.stderr
    assert _q(store, "select value from meta where key='schema_version'") == [("1",)]  # ничего не тронуто


def test_migration_keeps_records_provenance_history_and_open_episode(v1, cli_factory):
    store, repo = v1
    before = {t: _q(store, f"select count(*) from {t}")[0][0]
              for t in ("records", "evidence", "record_versions", "episodes", "episode_notes")}
    cli = cli_factory(repo, store, roots="app")
    rep = cli.json("migrate", session=False)
    assert rep["migrated"] and rep["from"] == 1 and rep["to"] == 2
    backup = Path(rep["backup"])
    assert backup.exists() and (backup.parent / "manifest.json").exists() and (backup.parent / "episodes").is_dir()
    relabelled = {r["id"]: (r["from"], r["to"]) for r in rep["relabelled"]}
    assert relabelled == {"decision_00001": ("verified", "sourced"), "fact_00002": ("high", "attested"),
                          "fact_00003": ("verified", "sourced")}
    assert rep["open_episodes"] == ["episode_000003"] and rep["archives_imported"] == 2

    after = {t: _q(store, f"select count(*) from {t}")[0][0] for t in before}
    assert after["records"] == before["records"] and after["evidence"] == before["evidence"]
    assert after["record_versions"] == before["record_versions"] + 3  # перемаркировка — новая версия, не правка
    assert after["episode_notes"] == before["episode_notes"] == 2
    assert _q(store, "select legacy_certainty from records where id='decision_00001'") == [("verified",)]

    # Эпизод открыт в рабочей копии, где создавался эталон (её имя — repo-7f9403); работаем от её имени.
    mem = Memory(store, repo, roots=("app",), scope="repo-7f9403")
    try:
        assert mem.get("fact_00001")["status_here"] == "superseded"
        assert mem.get("hypothesis_00001")["status_here"] == "invalidated"
        assert mem.get("decision_00001")["certainty_here"] == "sourced"  # прежний verified — не проверка
        versions = mem.store.record_versions("decision_00001")
        assert "a v1 'verified' meant 'source exists'" in versions[-1]["change"]
        assert versions[0]["snapshot"]["certainty"] == "verified"  # история прежней метки сохранена
        assert mem.why("fact_00002")["sources"][0]["kind"] == "user"

        # Открытый эпизод старой версии: сессия этой копии забирает его явно, заметки на месте.
        ctx = Context(mem)
        ctx.start_session("after-migration")
        with pytest.raises(Exception, match="belongs to"):
            ctx.note("not mine yet")
        adopted = ctx.adopt()
        assert adopted["adopted"] and adopted["episode"] == "episode_000003"
        res = ctx.end({"ops": []}, summary="gateway investigated after migration")
        assert res["saved"]
        archive = json.loads((store / "episodes" / "000003.json").read_text(encoding="utf-8"))
        assert [n["text"] for n in archive["notes"]][:2] == ["curl: 504 after 2.0s", "second observation"]
    finally:
        mem.close()

    again = cli.json("migrate", session=False)  # повторный запуск ничего не делает
    assert again["migrated"] is False and again["schema_version"] == 2


def test_restore_from_verified_backup(v1, cli_factory):
    store, repo = v1
    cli = cli_factory(repo, store, roots="app")
    rep = cli.json("migrate", session=False)
    restored = cli.json("migrate", "--restore", rep["backup"], session=False)
    assert Path(restored["previous_saved_as"]).exists()  # база v2 перед восстановлением сохранена
    assert restored["row_counts"] == rep["row_counts"]
    assert _q(store, "select value from meta where key='schema_version'") == [("1",)]
    assert cli.run("status", session=False).returncode == 5  # снова старая схема — снова шаг владельца
    again = cli.json("migrate", session=False)  # и переход проходит заново
    assert again["migrated"] is True


def test_corrupted_backup_is_refused(v1, cli_factory):
    store, repo = v1
    cli = cli_factory(repo, store, roots="app")
    rep = cli.json("migrate", session=False)
    bad = Path(rep["backup"])
    manifest = json.loads((bad.parent / "manifest.json").read_text(encoding="utf-8"))
    manifest["row_counts"]["records"] = 999
    (bad.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    out = cli.run("migrate", "--restore", str(bad), session=False)
    assert out.returncode == 2 and "row counts differ" in out.stderr
    assert _q(store, "select value from meta where key='schema_version'") == [("2",)]  # текущая база не тронута
