"""Переход хранилища со схемы 1 на 2. Только локальная база agent_memory.

Это шаг владельца: новый код сам старую базу не трогает (MigrationRequired), а другие
сессии со старым кодом в это время писать не должны.

Порядок:
1. Резервная копия через backup API SQLite (корректна и при WAL) плюс копия каталогов
   episodes/ и working/. Копия проверяется: integrity_check и совпадение числа строк
   в каждой таблице. Не прошла проверку — переход не начинается.
2. Одна транзакция: новые столбцы и таблицы, перенос рабочих файлов и архивов в базу,
   перемены статуса в журнал, честная перемаркировка уверенности. Ничего не удаляется.
3. Повторный запуск на базе версии 2 ничего не делает.

Перемаркировка: прежний verified ставился за наличие источника, проверкой он не был.
Он становится sourced (или attested / probable — по тому, какие доказательства есть),
прежняя метка сохраняется в legacy_certainty и в истории версий записи.

Восстановление: `migrate --restore <копия>` проверяет копию и записывает её поверх
текущей базы; текущая база перед этим сама сохраняется рядом (pre-restore).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import schema as S
from .store import KNOWLEDGE_DDL, now

VERIFIABLE = ("file", "code", "test", "doc", "commit")
ATTESTED = ("tool", "user")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _version(conn: sqlite3.Connection) -> int | None:
    if not conn.execute("select 1 from sqlite_master where name='records'").fetchone():
        return None
    row = conn.execute("select value from meta where key='schema_version'").fetchone()
    return int(row[0]) if row else 1


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        " and name not like '%_fts_%' and sql not like 'CREATE VIRTUAL%'")]
    return {t: conn.execute(f'select count(*) from "{t}"').fetchone()[0] for t in sorted(tables)}


def verify_backup(backup: Path, expected: dict[str, int] | None = None) -> dict:
    conn = _connect(backup)
    try:
        ok = conn.execute("pragma integrity_check").fetchone()[0]
        counts = _counts(conn)
    finally:
        conn.close()
    if ok != "ok":
        raise S.MemoryError_(f"backup {backup} failed integrity_check: {ok}")
    if expected is not None and counts != expected:
        raise S.MemoryError_(f"backup {backup} row counts differ from source: {counts} != {expected}")
    return counts


def make_backup(store_dir: Path) -> dict:
    db = store_dir / "memory.sqlite"
    dest_dir = store_dir / "backups" / f"v1-{_ts()}"
    dest_dir.mkdir(parents=True, exist_ok=False)
    dest = dest_dir / "memory.sqlite"
    src = _connect(db)
    try:
        expected = _counts(src)
        out = sqlite3.connect(str(dest))
        src.backup(out)
        out.close()
    finally:
        src.close()
    counts = verify_backup(dest, expected)
    for sub in ("episodes", "working"):
        if (store_dir / sub).is_dir():
            shutil.copytree(store_dir / sub, dest_dir / sub)
    manifest = {"source": str(db), "made_at": now(), "schema_version": 1, "row_counts": counts}
    (dest_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return {"backup": str(dest), "row_counts": counts}


def _add_column(conn: sqlite3.Connection, table: str, coldef: str) -> None:
    name = coldef.split()[0]
    cols = {r[1] for r in conn.execute(f'pragma table_info("{table}")')}
    if name not in cols:
        conn.execute(f'alter table "{table}" add column {coldef}')


def _ddl_statements() -> list[str]:
    body = "\n".join(ln for ln in KNOWLEDGE_DDL.splitlines() if not ln.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def _new_certainty(old: str, kinds: set[str]) -> str:
    if old in ("superseded", "contradicted", "hypothesis", "probable", "unknown"):
        return old
    if kinds & set(VERIFIABLE):
        return "sourced"
    if kinds & (set(ATTESTED) | {"url"}):  # как в v2: внешняя страница — засвидетельствованное
        return "attested"
    return "probable"


def migrate(store_dir: Path) -> dict:
    store_dir = Path(store_dir)
    db = store_dir / "memory.sqlite"
    if not db.exists():
        raise S.MemoryError_(f"no store at {db}")
    conn = _connect(db)
    try:
        version = _version(conn)
    finally:
        conn.close()
    if version is None:
        raise S.MemoryError_(f"{db} is not an agent_memory store")
    if version >= S.SCHEMA_VERSION:
        return {"migrated": False, "schema_version": version, "note": "already at the current schema; nothing done"}

    backup = make_backup(store_dir)
    conn = _connect(db)
    report = {"migrated": True, "from": version, "to": S.SCHEMA_VERSION, **backup,
              "relabelled": [], "transitions": 0, "working_imported": [], "archives_imported": 0,
              "legacy_code_edges_closed": 0}
    try:
        conn.execute("begin immediate")
        for coldef in ("observed_commit text", "observed_branch text", "observed_scope text",
                       "observed_dirty integer not null default 0", "observed_patch text", "legacy_certainty text"):
            _add_column(conn, "records", coldef)
        for coldef in ("category text", "excerpt text", "verbatim integer not null default 0", "accessed_at text",
                       "version_label text", "check_method text", "check_condition text", "check_expect text",
                       "check_result text", "check_output text", "check_target_hash text", "check_commit text",
                       "check_dirty integer", "checked_at text", "check_origin text", "check_patch text"):
            _add_column(conn, "evidence", coldef)
        for coldef in ("scope text not null default ''", "owner_token text", "diff_id text", "archive_json text",
                       "archive_exported_at text"):
            _add_column(conn, "episodes", coldef)
        for stmt in _ddl_statements():  # новые таблицы; существующие не трогаются (if not exists)
            conn.execute(stmt)

        category = {k: v for k, v in S.EVIDENCE_CATEGORY.items()}
        for kind, cat in category.items():
            conn.execute("update evidence set category=? where kind=? and category is null", (cat, kind))

        # Честная перемаркировка: прежний verified/high — не проверка.
        for r in conn.execute("select * from records").fetchall():
            kinds = {e[0] for e in conn.execute("select kind from evidence where record_id=?", (r["id"],))}
            new = _new_certainty(r["certainty"], kinds)
            if new != r["certainty"]:
                conn.execute("update records set certainty=?, legacy_certainty=?, version=version+1, updated_at=?"
                             " where id=?", (new, r["certainty"], now(), r["id"]))
                snap = dict(conn.execute("select * from records where id=?", (r["id"],)).fetchone())
                snap.pop("n", None)
                snap["tags"] = json.loads(snap.get("tags") or "[]")
                conn.execute(
                    "insert into record_versions(record_id, version, snapshot, change, episode, at)"
                    " values(?,?,?,?,?,?)",
                    (r["id"], snap["version"], json.dumps(snap, ensure_ascii=False),
                     f"migrated v1->v2: certainty {r['certainty']} -> {new} (v1 label kept in legacy_certainty; "
                     "a v1 'verified' meant 'source exists', not a check)", None, now()))
                report["relabelled"].append({"id": r["id"], "from": r["certainty"], "to": new})

        # Статусы v1 — в журнал перемен как глобальные (коммит неизвестен): действуют везде, как раньше.
        for r in conn.execute("select id, status, valid_until, superseded_by from records where status!='active'"):
            exists = conn.execute("select 1 from transitions where record_id=?", (r["id"],)).fetchone()
            if not exists:
                conn.execute(
                    "insert into transitions(record_id, kind, at_commit, scope, dirty, episode, by_record, reason, at)"
                    " values(?,?,?,?,?,?,?,?,?)",
                    (r["id"], r["status"], None, None, 0, r["valid_until"], r["superseded_by"],
                     "migrated from v1 status", now()))
                report["transitions"] += 1

        # Граф кода v1 был общим на все копии. В v2 он у каждой копии свой: старые рёбра закрываем, не удаляя.
        cur = conn.execute("update edges set active=0, ended_at=?, note=coalesce(note || '; ', '') || ?"
                           " where active=1 and origin='deterministic'",
                           (now(), "v2: code graph moved to the per-worktree database"))
        report["legacy_code_edges_closed"] = cur.rowcount

        # working.md v1 был файлом — переносим текст в базу; файл остаётся.
        for f in sorted((store_dir / "working").glob("*.md")) if (store_dir / "working").is_dir() else []:
            text = f.read_text(encoding="utf-8")
            conn.execute("insert into working_state(scope, text, updated_at, file_synced_hash) values(?,?,?,?)"
                         " on conflict(scope) do nothing",
                         (f.stem, text, now(), hashlib.sha1(text.encode("utf-8")).hexdigest()))
            report["working_imported"].append(f.stem)

        # Архивы v1 — каноническим содержимым эпизода в базе.
        for ep in conn.execute("select id, archive_path from episodes where status!='open' and archive_json is null"):
            path = store_dir / (ep["archive_path"] or "")
            if ep["archive_path"] and path.is_file():
                conn.execute("update episodes set archive_json=?, archive_exported_at=? where id=?",
                             (path.read_text(encoding="utf-8"), now(), ep["id"]))
                report["archives_imported"] += 1

        conn.execute("insert into meta(key, value) values('schema_version', ?)"
                     " on conflict(key) do update set value=excluded.value", (str(S.SCHEMA_VERSION),))
        conn.execute("insert into meta(key, value) values('migrated_from_v1', ?)"
                     " on conflict(key) do update set value=excluded.value",
                     (json.dumps({"at": now(), "backup": backup["backup"]}),))
        conn.execute("commit")
    except BaseException:
        if conn.in_transaction:
            conn.execute("rollback")
        raise
    finally:
        conn.close()
    open_eps = _connect(db).execute("select id from episodes where status='open'").fetchall()
    report["open_episodes"] = [r[0] for r in open_eps]
    if report["open_episodes"]:
        report["note"] = ("open episode(s) kept with their notes; they have no owner session yet: the session "
                          "that holds the worktree lease takes one over with `session adopt`")
    return report


def restore(store_dir: Path, backup: Path) -> dict:
    """Записать проверенную копию поверх текущей базы. Текущая база сначала сохраняется рядом."""
    store_dir = Path(store_dir)
    backup = Path(backup)
    manifest_path = backup.parent / "manifest.json"
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))["row_counts"] if manifest_path.exists() else None
    counts = verify_backup(backup, expected)
    db = store_dir / "memory.sqlite"
    keep = store_dir / "backups" / f"pre-restore-{_ts()}.sqlite"
    keep.parent.mkdir(parents=True, exist_ok=True)
    cur = _connect(db)
    try:
        tmp = sqlite3.connect(str(keep))
        cur.backup(tmp)
        tmp.close()
        src = _connect(backup)
        try:
            src.backup(cur)
        finally:
            src.close()
    finally:
        cur.close()
    for sub in ("episodes", "working"):
        if (backup.parent / sub).is_dir():
            for f in (backup.parent / sub).iterdir():
                target = store_dir / sub / f.name
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
    after = verify_backup(db, counts)
    return {"restored_from": str(backup), "previous_saved_as": str(keep), "row_counts": after}
