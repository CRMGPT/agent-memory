"""Сбои при завершении эпизода и восстановление в НОВОМ процессе, без ручного удаления.

Точки сбоя (agent_memory/faults.py): до записи, ровно перед COMMIT, обрыв процесса до и
после COMMIT, отказ записи JSON-архива, отказ записи working.md. После каждого сбоя —
новый процесс CLI, и состояние проверяется на согласованность: записи, заметки, архив.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

DIFF = json.dumps({"ops": [{"op": "ADD", "record": {
    "type": "FACT", "section": "code", "statement": "Refresh tokens rotate on every use",
    "evidence": [{"kind": "doc", "ref": "docs/auth.md", "locator": "L3-L3"}]}}]})


def _open_with_notes(cli):
    cli.start()
    assert cli.run("begin", "investigate rotation").returncode == 0
    cli.json("note", "first raw output")
    cli.json("note", "second raw output")


def _state(store):
    db = sqlite3.connect(store / "memory.sqlite")
    try:
        return {
            "facts": db.execute("select count(*) from records where type='FACT'").fetchone()[0],
            "summaries": db.execute("select count(*) from records where type='EPISODE'").fetchone()[0],
            "episode": db.execute("select status, archive_json is not null, archive_exported_at is not null"
                                  " from episodes where id='episode_000001'").fetchone(),
            "notes_rows": db.execute("select count(*) from episode_notes").fetchone()[0],
            "applied_diffs": db.execute("select count(*) from applied_diffs").fetchone()[0],
        }
    finally:
        db.close()


def _consistent_after_success(store):
    st = _state(store)
    assert st["facts"] == 1 and st["summaries"] == 1  # ни потерь, ни дублей
    assert st["episode"] == ("committed", 1, 1) and st["notes_rows"] == 0
    archive = json.loads((store / "episodes" / "000001.json").read_text(encoding="utf-8"))
    assert [n["text"] for n in archive["notes"]] == ["first raw output", "second raw output"]
    assert archive["commit_report"]["added"][0] == "fact_00001"


def _end(cli, **kw):
    return cli.run("end", "--diff", DIFF, "--summary", "rotation understood", **kw)


@pytest.mark.parametrize("fault", ["end_before_commit", "end_commit", "end_before_commit:exit", "end_commit:exit"])
def test_failure_before_commit_saves_nothing_and_retry_succeeds(repo, cli_factory, tmp_path, fault):
    store = tmp_path / "store"
    cli = cli_factory(repo, store)
    _open_with_notes(cli)
    out = _end(cli, fault=fault)
    if fault.endswith(":exit"):
        assert out.returncode == 91  # процесс убит посреди транзакции
    else:
        assert out.returncode == 2 and "nothing saved" in out.stderr
    st = _state(store)
    assert st["facts"] == 0 and st["summaries"] == 0 and st["applied_diffs"] == 0
    assert st["episode"][0] == "open" and st["notes_rows"] == 2  # заметки на месте
    assert not (store / "episodes" / "000001.json").exists()
    retry = _end(cli)  # новый процесс, без ручной уборки
    assert retry.returncode == 0, retry.stderr
    _consistent_after_success(store)


def test_crash_after_commit_is_repaired_and_retry_does_not_duplicate(repo, cli_factory, tmp_path):
    store = tmp_path / "store"
    cli = cli_factory(repo, store)
    _open_with_notes(cli)
    out = _end(cli, fault="end_after_commit:exit")
    assert out.returncode == 91
    st = _state(store)
    assert st["facts"] == 1 and st["episode"] == ("committed", 1, 0)  # база сохранена, файла ещё нет
    assert not (store / "episodes" / "000001.json").exists()
    status = cli.run("status")  # любая следующая команда восстанавливает производный файл
    assert status.returncode == 0 and "restored archive" in status.stderr
    retry = _end(cli)  # повтор того же end: результат прежний, записей не прибавилось
    assert retry.returncode == 0, retry.stderr
    assert json.loads(retry.stdout)["replayed"] is True
    _consistent_after_success(store)


def test_archive_export_failure_reports_saved_and_repair_restores(repo, cli_factory, tmp_path):
    store = tmp_path / "store"
    cli = cli_factory(repo, store)
    _open_with_notes(cli)
    out = _end(cli, fault="export_archive")
    assert out.returncode == 6 and "database SAVED" in out.stderr and "repair" in out.stderr
    assert not (store / "episodes" / "000001.json").exists()
    rep = cli.json("repair")
    assert rep["archives_exported"] == ["episode_000001"]
    _consistent_after_success(store)


def test_working_file_failure_reports_saved_and_is_restored(repo, cli_factory, tmp_path):
    store = tmp_path / "store"
    cli = cli_factory(repo, store)
    _open_with_notes(cli)
    new_working = tmp_path / "working.md"
    new_working.write_text("# Goal\nship rotation fix\n", encoding="utf-8")
    out = cli.run("end", "--diff", DIFF, "--summary", "rotation understood", "--working", str(new_working),
                  fault="write_working")
    assert out.returncode == 6 and "working.md" in out.stderr and "database SAVED" in out.stderr
    assert cli.run("working", "show").stdout.startswith("# Goal\nship rotation fix")  # канон — в базе
    files = list((store / "working").glob("*.md"))
    assert files and "ship rotation fix" in files[0].read_text(encoding="utf-8")  # следующая команда дописала файл
    _consistent_after_success(store)


def test_drop_after_crash_is_safe(repo, cli_factory, tmp_path):
    store = tmp_path / "store"
    cli = cli_factory(repo, store)
    _open_with_notes(cli)
    assert cli.run("drop", "--reason", "wrong path", fault="end_commit:exit").returncode == 91
    assert _state(store)["episode"][0] == "open" and _state(store)["notes_rows"] == 2
    assert cli.run("drop", "--reason", "wrong path").returncode == 0
    archive = json.loads((store / "episodes" / "000001.json").read_text(encoding="utf-8"))
    assert archive["status"] == "dropped" and len(archive["notes"]) == 2
