"""Хранилище памяти: две базы SQLite.

* Общая база знаний `memory.sqlite` — записи, версии, доказательства, смысловые рёбра,
  эпизоды, аренда записи, рабочее состояние, применённые диффы. Её делят все рабочие копии.
* База рабочей копии `worktrees/<scope>/code.sqlite` — граф кода, кэш файлов, пометки
  «устарело». Это производное состояние кода одной рабочей копии: её индексирует только
  она, и другая ветка его не трогает.

Базы на разных соединениях. Долгое индексирование держит блокировку записи только своей
базы кода и не мешает соседней сессии писать знания. Весь SQL живёт здесь.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote

from . import faults
from .schema import SCHEMA_VERSION, MemoryError_, MigrationRequired

CODE_SCHEMA_VERSION = 1

KNOWLEDGE_DDL = """
create table if not exists meta(key text primary key, value text);
create table if not exists counters(prefix text primary key, n integer not null);

create table if not exists records(
    n integer primary key autoincrement,
    id text not null unique,
    type text not null,
    statement text not null,
    reason text,
    subject text,
    status text not null,
    certainty text not null,
    confidence real,
    tags text not null default '[]',
    created_from text,
    valid_from text,
    valid_until text,
    superseded_by text,
    created_at text not null,
    updated_at text not null,
    version integer not null default 1,
    observed_commit text,
    observed_branch text,
    observed_scope text,
    observed_dirty integer not null default 0,
    observed_patch text,
    legacy_certainty text,
    section text
);
create index if not exists records_subject on records(subject);

create table if not exists record_versions(
    record_id text not null,
    version integer not null,
    snapshot text not null,
    change text not null,
    episode text,
    at text not null,
    primary key(record_id, version)
);

create table if not exists evidence(
    id integer primary key autoincrement,
    record_id text not null,
    kind text not null,
    ref text not null,
    locator text,
    excerpt_hash text,
    note text,
    episode text,
    created_at text not null,
    category text,
    excerpt text,
    verbatim integer not null default 0,
    accessed_at text,
    version_label text,
    check_method text,
    check_condition text,
    check_expect text,
    check_result text,
    check_output text,
    check_target_hash text,
    check_commit text,
    check_dirty integer,
    checked_at text,
    check_origin text,
    check_patch text
);
create index if not exists evidence_record on evidence(record_id);
create index if not exists evidence_ref on evidence(ref);

-- Узлы знаний: записи, эпизоды, понятия, внешние системы из диффов. Узлы кода — в базе рабочей копии.
create table if not exists nodes(
    n integer primary key autoincrement,
    id text not null unique,
    kind text not null,
    label text not null,
    path text,
    start_line integer,
    end_line integer,
    body_hash text,
    status text not null default 'present',
    updated_at text
);

-- Смысловые рёбра (asserted / inferred). Детерминированные — в базе рабочей копии.
create table if not exists edges(
    id integer primary key autoincrement,
    src text not null,
    rel text not null,
    dst text not null,
    origin text not null,
    confidence real,
    episode text,
    active integer not null default 1,
    note text,
    created_at text not null,
    ended_at text
);
create index if not exists edges_src on edges(src, active);
create index if not exists edges_dst on edges(dst, active);

create table if not exists vectors(record_id text primary key, vec blob not null);

create virtual table if not exists records_fts using fts5(
    statement, reason, subject, tags, record_id unindexed, tokenize='porter unicode61'
);
create virtual table if not exists nodes_fts using fts5(terms, node_id unindexed);

-- Перемены статуса записи. Действуют там, где коммит перемены входит в историю рабочей копии.
create table if not exists transitions(
    id integer primary key autoincrement,
    record_id text not null,
    kind text not null,
    at_commit text,
    branch text,
    scope text,
    dirty integer not null default 0,
    patch text,
    episode text,
    by_record text,
    reason text,
    at text not null
);
create index if not exists transitions_record on transitions(record_id);

create table if not exists episodes(
    id text primary key,
    task text not null,
    frame text not null,
    status text not null,
    started_at text not null,
    ended_at text,
    injected text not null default '[]',
    archive_path text,
    summary text,
    scope text not null default '',
    owner_token text,
    diff_id text,
    archive_json text,
    archive_exported_at text
);

create table if not exists episode_notes(
    episode_id text not null,
    seq integer not null,
    kind text not null,
    text text not null,
    ref text,
    at text not null,
    primary key(episode_id, seq)
);

-- Дифф, применённый один раз: повтор с тем же id возвращает прежний результат.
create table if not exists applied_diffs(
    diff_id text primary key,
    content_hash text not null,
    episode text,
    result text not null,
    at text not null
);

-- Аренда записи: одна сессия пишет из одной рабочей копии.
-- owner_proc — JSON владельца: процесс Claude Code (pid, время создания, компьютер) или причина,
--   почему он не записан; holder_proc — запись держателя прежней версии (только для finish),
-- history — JSON передач аренды при восстановлении (кто, кому, почему).
create table if not exists leases(
    scope text primary key,
    token text not null,
    holder text,
    acquired_at text not null,
    heartbeat_at text not null,
    ttl_s integer not null,
    previous_token text,
    holder_proc text,
    history text,
    owner_proc text
);

-- Каноническое рабочее состояние (working.md). Файл на диске — его производная копия.
create table if not exists working_state(
    scope text primary key,
    text text not null,
    updated_at text not null,
    file_synced_hash text
);

create table if not exists metrics(
    id integer primary key autoincrement,
    at text not null,
    episode_id text,
    event text not null,
    data text not null
);
"""

CODE_DDL = """
create table if not exists meta(key text primary key, value text);
create table if not exists nodes(
    n integer primary key autoincrement,
    id text not null unique,
    kind text not null,
    label text not null,
    path text,
    start_line integer,
    end_line integer,
    body_hash text,
    status text not null default 'present',
    updated_at text
);
create index if not exists nodes_path on nodes(path);
create index if not exists nodes_body on nodes(body_hash);
create table if not exists edges(
    id integer primary key autoincrement,
    src text not null,
    rel text not null,
    dst text not null,
    origin text not null,
    confidence real,
    episode text,
    active integer not null default 1,
    note text,
    created_at text not null,
    ended_at text
);
create index if not exists edges_src on edges(src, active);
create index if not exists edges_dst on edges(dst, active);
create virtual table if not exists nodes_fts using fts5(terms, node_id unindexed);
create table if not exists files(
    path text primary key,
    sha text not null,
    mtime real,
    size integer,
    indexed_at text
);
create table if not exists stale_marks(
    record_id text not null,
    node_id text not null default '',
    reason text not null,
    at text not null,
    primary key(record_id, node_id)
);
"""

RECORD_FIELDS = (
    "id", "type", "statement", "reason", "subject", "status", "certainty", "confidence", "tags",
    "created_from", "valid_from", "valid_until", "superseded_by",
    "created_at", "updated_at", "version",
    "observed_commit", "observed_branch", "observed_scope", "observed_dirty", "observed_patch",
    "legacy_certainty", "section",
)
EVIDENCE_FIELDS = (
    "kind", "ref", "locator", "excerpt_hash", "note", "category", "excerpt", "verbatim", "accessed_at",
    "version_label", "check_method", "check_condition", "check_expect", "check_result", "check_output",
    "check_target_hash", "check_commit", "check_dirty", "checked_at", "check_origin", "check_patch",
)
CODE_PREFIXES = ("code:", "file:")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_code_id(nid: str) -> bool:
    return nid.startswith(CODE_PREFIXES)


class _Db:
    """Соединение с одной базой: транзакции, узлы, рёбра."""

    def __init__(self, path: Path | None, ddl: str, read_only: bool = False, timeout: float = 30.0):
        """read_only: только чтение — `file:...?mode=ro` (свежие данные журнала WAL видны, не
        immutable), query_only, каталог и файл не создаются; любая попытка записи — ошибка SQLite.
        path=None — пустая база в памяти с этой схемой (например, «карты кода нет»)."""
        self._ddl = ddl
        self._tx_depth = 0
        self.read_only = read_only
        if path is None:
            self.path = None
            self.conn = sqlite3.connect(":memory:", isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(ddl)
            self.conn.execute("pragma query_only=1")
            return
        self.path = Path(path)
        if read_only:
            uri = "file:" + quote(self.path.resolve().as_posix(), safe="/:") + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=timeout)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"pragma busy_timeout={int(timeout * 1000)}")
            self.conn.execute("pragma query_only=1")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=timeout)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma busy_timeout=30000")

    def ensure_schema(self) -> None:
        self.conn.executescript(self._ddl)

    def meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "select value from meta where key=?", (key,)
        ).fetchone() if self._has_table("meta") else None
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "insert into meta(key, value) values(?,?) on conflict(key) do update set value=excluded.value",
            (key, value),
        )

    def _has_table(self, name: str) -> bool:
        return bool(self.conn.execute("select 1 from sqlite_master where name=?", (name,)).fetchone())

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self, commit_fault: str | None = None) -> Iterator[None]:
        """Вложенные вызовы — одна транзакция: всё или ничего.

        commit_fault — имя точки искусственного сбоя прямо перед COMMIT (faults.py), только для проверок.
        """
        if self._tx_depth == 0:
            self.conn.execute("begin immediate")
        self._tx_depth += 1
        try:
            yield
            if self._tx_depth == 1 and commit_fault:
                faults.hit(commit_fault)
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.conn.execute("rollback")
            raise
        else:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.conn.execute("commit")

    # --- узлы -----------------------------------------------------------

    def _rowid(self, table: str, key: str) -> int:
        # Строка FTS живёт под rowid строки-владельца: удаление по rowid мгновенное.
        return self.conn.execute(f"select rowid from {table} where id=?", (key,)).fetchone()[0]

    def upsert_node(self, node: dict[str, Any]) -> None:
        self.conn.execute(
            "insert into nodes(id, kind, label, path, start_line, end_line, body_hash, status, updated_at)"
            " values(:id, :kind, :label, :path, :start_line, :end_line, :body_hash, :status, :updated_at)"
            " on conflict(id) do update set kind=excluded.kind, label=excluded.label, path=excluded.path,"
            " start_line=excluded.start_line, end_line=excluded.end_line, body_hash=excluded.body_hash,"
            " status=excluded.status, updated_at=excluded.updated_at",
            {
                "path": None, "start_line": None, "end_line": None, "body_hash": None,
                "status": "present", "updated_at": now(), **node,
            },
        )
        if node.get("terms") is not None:
            rowid = self._rowid("nodes", node["id"])
            self.conn.execute("delete from nodes_fts where rowid=?", (rowid,))
            self.conn.execute("insert into nodes_fts(rowid, terms, node_id) values(?,?,?)",
                              (rowid, node["terms"], node["id"]))

    def delete_node(self, nid: str) -> None:
        row = self.conn.execute("select rowid from nodes where id=?", (nid,)).fetchone()
        if row:
            self.conn.execute("delete from nodes_fts where rowid=?", (row[0],))
            self.conn.execute("delete from nodes where id=?", (nid,))

    def get_node(self, nid: str) -> dict | None:
        row = self.conn.execute("select * from nodes where id=?", (nid,)).fetchone()
        if not row:
            return None
        node = dict(row)
        node.pop("n", None)
        return node

    def set_node_status(self, nid: str, status: str) -> None:
        self.conn.execute("update nodes set status=?, updated_at=? where id=?", (status, now(), nid))

    def fts_nodes(self, query: str, limit: int) -> list[tuple[str, float]]:
        if not query:
            return []
        rows = self.conn.execute(
            "select node_id, bm25(nodes_fts) as s from nodes_fts where nodes_fts match ? order by s limit ?",
            (query, limit),
        ).fetchall()
        return [(r["node_id"], -float(r["s"])) for r in rows]

    # --- рёбра ----------------------------------------------------------

    def add_edge(self, src: str, rel: str, dst: str, origin: str, **kw: Any) -> int:
        existing = self.conn.execute(
            "select id from edges where src=? and rel=? and dst=? and origin=? and active=1",
            (src, rel, dst, origin),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = self.conn.execute(
            "insert into edges(src, rel, dst, origin, confidence, episode, note, created_at)"
            " values(?,?,?,?,?,?,?,?)",
            (src, rel, dst, origin, kw.get("confidence"), kw.get("episode"), kw.get("note"), now()),
        )
        return int(cur.lastrowid)

    def end_edges(self, where: dict[str, Any], note: str) -> int:
        """Ребро не удаляется, а закрывается: история связей остаётся."""
        cond = " and ".join(f"{k}=?" for k in where)
        cur = self.conn.execute(
            f"update edges set active=0, ended_at=?, note=coalesce(note || '; ', '') || ? where active=1 and {cond}",
            [now(), note, *where.values()],
        )
        return cur.rowcount

    def edges_touching(self, nids: Iterable[str]) -> list[dict]:
        ids = list(dict.fromkeys(nids))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"select * from edges where active=1 and (src in ({marks}) or dst in ({marks}))", [*ids, *ids]
        ).fetchall()
        if isinstance(self, KnowledgeDb):  # граф кода живёт только в базе копии; старые общие рёбра v1 не читаем
            rows = [r for r in rows if r["origin"] != "deterministic"]
        return [dict(r) for r in rows]

    def edges_where(self, **where: Any) -> list[dict]:
        cond = " and ".join(f"{k}=?" for k in where) or "1=1"
        rows = self.conn.execute(f"select * from edges where {cond}", list(where.values())).fetchall()
        return [dict(r) for r in rows]


class KnowledgeDb(_Db):
    def __init__(self, path: Path, read_only: bool = False, timeout: float = 30.0):
        super().__init__(path, KNOWLEDGE_DDL, read_only, timeout)

    def schema_version(self) -> int | None:
        """None — базы ещё нет (пустой файл)."""
        if not self._has_table("records"):
            return None
        value = self.meta("schema_version")
        return int(value) if value else 1


class CodeDb(_Db):
    def __init__(self, path: Path | None, read_only: bool = False, timeout: float = 30.0):
        super().__init__(path, CODE_DDL, read_only, timeout)


class SqliteStore:
    """Фасад над двумя базами. Чтение графа объединяет обе, запись идёт по назначению."""

    def __init__(self, store_dir: Path, scope: str, allow_old: bool = False, read_only: bool = False,
                 timeout: float = 30.0):
        self.dir = Path(store_dir)
        self.scope = scope
        self.read_only = read_only
        self.code_path = self.dir / "worktrees" / scope / "code.sqlite"
        if read_only:
            # Читающий путь: ничего не создаётся и не мигрирует. Нет базы, другая схема — отказ;
            # нет карты кода этой копии — пустая карта в памяти (code_map_available = False).
            if not (self.dir / "memory.sqlite").is_file():
                raise MemoryError_(f"no memory store at {self.dir}")
            self.k = KnowledgeDb(self.dir / "memory.sqlite", read_only=True, timeout=timeout)
            version = self.k.schema_version()
            if version != SCHEMA_VERSION:
                self.k.close()
                raise MigrationRequired(f"store {self.dir} is schema v{version}; the executor runs `migrate`")
            self.code_map_available = self.code_path.is_file()
            self.c = CodeDb(self.code_path if self.code_map_available else None, read_only=True, timeout=timeout)
            return
        self.code_map_available = True
        self.k = KnowledgeDb(self.dir / "memory.sqlite")
        version = self.k.schema_version()
        if version is None:
            self.k.ensure_schema()
            self.k.set_meta("schema_version", str(SCHEMA_VERSION))
        elif version < SCHEMA_VERSION and not allow_old:
            self.k.close()
            raise MigrationRequired(
                f"store {self.dir} is schema v{version}; this code needs v{SCHEMA_VERSION}. "
                "Owner step: stop other sessions, then run `python3 -m agent_memory migrate` "
                "(it makes and verifies a backup first)."
            )
        elif version == SCHEMA_VERSION:
            self.k.ensure_schema()
            self._ensure_columns()
        self.code_path = self.dir / "worktrees" / scope / "code.sqlite"
        self.c = CodeDb(self.code_path)
        self.c.ensure_schema()
        if self.c.meta("code_schema_version") is None:
            self.c.set_meta("code_schema_version", str(CODE_SCHEMA_VERSION))

    # Столбцы, добавленные внутри схемы 2 после её первой сборки: база ранней сборки
    # получает их при открытии, без перехода и без потери данных.
    LATE_COLUMNS = (("records", "observed_patch text"), ("evidence", "check_patch text"),
                    ("transitions", "patch text"), ("leases", "holder_proc text"), ("leases", "history text"),
                    ("leases", "owner_proc text"), ("records", "section text"))

    def _ensure_columns(self) -> None:
        for table, coldef in self.LATE_COLUMNS:
            cols = {r[1] for r in self.k.conn.execute(f'pragma table_info("{table}")')}
            if coldef.split()[0] not in cols:
                self.k.conn.execute(f'alter table "{table}" add column {coldef}')

    @property
    def conn(self) -> sqlite3.Connection:
        """Соединение общей базы знаний (для простых запросов отчётов)."""
        return self.k.conn

    def close(self) -> None:
        self.k.close()
        self.c.close()

    def transaction(self, commit_fault: str | None = None):
        return self.k.transaction(commit_fault=commit_fault)

    def code_transaction(self):
        return self.c.transaction()

    # --- идентификаторы -------------------------------------------------

    def next_id(self, prefix: str) -> str:
        with self.transaction():
            row = self.k.conn.execute("select n from counters where prefix=?", (prefix,)).fetchone()
            n = (row["n"] if row else 0) + 1
            self.k.conn.execute(
                "insert into counters(prefix, n) values(?, ?) on conflict(prefix) do update set n=excluded.n",
                (prefix, n),
            )
        width = 6 if prefix == "episode" else 5
        return f"{prefix}_{n:0{width}d}"

    # --- записи ---------------------------------------------------------

    def _row_to_record(self, row: sqlite3.Row) -> dict:
        rec = dict(row)
        rec.pop("n", None)
        rec["tags"] = json.loads(rec.get("tags") or "[]")
        reasons = self.stale_reasons(rec["id"])
        rec["stale_reason"] = "; ".join(reasons) if reasons else None
        return rec

    def _snapshot(self, rid: str, change: str, episode: str | None) -> None:
        rec = self.get_record(rid)
        assert rec is not None
        rec.pop("stale_reason", None)  # производное состояние рабочей копии в историю не пишем
        self.k.conn.execute(
            "insert into record_versions(record_id, version, snapshot, change, episode, at) values(?,?,?,?,?,?)",
            (rid, rec["version"], json.dumps(rec, ensure_ascii=False), change, episode, now()),
        )

    def _reindex_fts(self, rec: dict) -> None:
        rowid = self.k._rowid("records", rec["id"])
        self.k.conn.execute("delete from records_fts where rowid=?", (rowid,))
        self.k.conn.execute(
            "insert into records_fts(rowid, statement, reason, subject, tags, record_id) values(?,?,?,?,?,?)",
            (
                rowid,
                rec["statement"],
                rec.get("reason") or "",
                (rec.get("subject") or "").replace(".", " ").replace("_", " "),
                " ".join(rec.get("tags") or []),
                rec["id"],
            ),
        )

    def insert_record(self, rec: dict[str, Any], vec: bytes | None, episode: str | None) -> None:
        with self.transaction():
            row = {k: rec.get(k) for k in RECORD_FIELDS}
            row["tags"] = json.dumps(rec.get("tags") or [], ensure_ascii=False)
            row["version"] = 1
            row["observed_dirty"] = int(bool(rec.get("observed_dirty")))
            cols = ",".join(RECORD_FIELDS)
            marks = ",".join("?" for _ in RECORD_FIELDS)
            self.k.conn.execute(f"insert into records({cols}) values({marks})", [row[k] for k in RECORD_FIELDS])
            self._reindex_fts(rec)
            if vec is not None:
                self.k.conn.execute("insert or replace into vectors(record_id, vec) values(?,?)", (rec["id"], vec))
            self.k.upsert_node({"id": rec["id"], "kind": "record", "label": rec["statement"][:120],
                                "status": "present"})
            self._snapshot(rec["id"], "created", episode)

    def update_record(self, rid: str, patch: dict[str, Any], change: str, episode: str | None) -> dict:
        with self.transaction():
            cur = self.get_record(rid)
            if cur is None:
                raise KeyError(rid)
            unknown = {k for k in patch if k not in RECORD_FIELDS}
            if unknown:
                raise ValueError(f"unknown record fields {sorted(unknown)}")
            fields = {k: v for k, v in patch.items() if k not in ("id", "version")}
            if "tags" in fields:
                fields["tags"] = json.dumps(fields["tags"] or [], ensure_ascii=False)
            fields["updated_at"] = now()
            fields["version"] = cur["version"] + 1
            sets = ",".join(f"{k}=?" for k in fields)
            self.k.conn.execute(f"update records set {sets} where id=?", [*fields.values(), rid])
            new = self.get_record(rid)
            assert new is not None
            self._reindex_fts(new)
            self._snapshot(rid, change, episode)
            return new

    def set_vector(self, rid: str, vec: bytes) -> None:
        self.k.conn.execute("insert or replace into vectors(record_id, vec) values(?,?)", (rid, vec))

    def get_record(self, rid: str) -> dict | None:
        row = self.k.conn.execute("select * from records where id=?", (rid,)).fetchone()
        return self._row_to_record(row) if row else None

    def records_by_subject(self, subject: str) -> list[dict]:
        rows = self.k.conn.execute("select * from records where subject=? order by n", (subject,)).fetchall()
        return [self._row_to_record(r) for r in rows]

    def records_of_type(self, rtype: str) -> Iterable[dict]:
        for r in self.k.conn.execute("select * from records where type=? order by n", (rtype,)).fetchall():
            yield self._row_to_record(r)

    def record_versions(self, rid: str) -> list[dict]:
        rows = self.k.conn.execute(
            "select version, change, episode, at, snapshot from record_versions where record_id=? order by version",
            (rid,),
        ).fetchall()
        return [dict(r) | {"snapshot": json.loads(r["snapshot"])} for r in rows]

    def iter_records(self) -> Iterable[dict]:
        for r in self.k.conn.execute("select * from records order by n").fetchall():
            yield self._row_to_record(r)

    def count_records(self) -> int:
        return self.k.conn.execute("select count(*) from records").fetchone()[0]

    # --- перемены статуса -------------------------------------------------

    def add_transition(self, rid: str, kind: str, observed: dict, episode: str | None, by: str | None,
                       reason: str | None) -> None:
        self.k.conn.execute(
            "insert into transitions(record_id, kind, at_commit, branch, scope, dirty, patch, episode, by_record,"
            " reason, at) values(?,?,?,?,?,?,?,?,?,?,?)",
            (rid, kind, observed.get("commit"), observed.get("branch"), observed.get("scope"),
             int(bool(observed.get("dirty"))), observed.get("patch"), episode, by, reason, now()),
        )

    def transitions(self, rids: Iterable[str] | None = None) -> dict[str, list[dict]]:
        if rids is None:
            rows = self.k.conn.execute("select * from transitions order by id").fetchall()
        else:
            ids = list(dict.fromkeys(rids))
            if not ids:
                return {}
            marks = ",".join("?" for _ in ids)
            rows = self.k.conn.execute(
                f"select * from transitions where record_id in ({marks}) order by id", ids
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["record_id"], []).append(dict(r))
        return out

    def has_pending(self, scope: str) -> bool:
        return bool(self.k.conn.execute(
            "select 1 from records where observed_scope=? and observed_dirty=1 union all "
            "select 1 from transitions where scope=? and dirty=1 limit 1", (scope, scope)).fetchone())

    def anchor_pending(self, scope: str, can_anchor, new_commit: str) -> int:
        """Записи и перемены этой копии на незакоммиченных правках привязать к коммиту,
        если именно эти правки закоммичены (решает can_anchor(commit, patch))."""
        n = 0
        rows = self.k.conn.execute(
            "select id, observed_commit, observed_patch from records where observed_scope=? and observed_dirty=1",
            (scope,)).fetchall()
        for r in rows:
            if can_anchor(r["observed_commit"], r["observed_patch"]):
                self.k.conn.execute("update records set observed_dirty=0, observed_commit=? where id=?",
                                    (new_commit, r["id"]))
                n += 1
        rows = self.k.conn.execute(
            "select id, at_commit, patch from transitions where scope=? and dirty=1", (scope,)).fetchall()
        for r in rows:
            if can_anchor(r["at_commit"], r["patch"]):
                self.k.conn.execute("update transitions set dirty=0, at_commit=? where id=?", (new_commit, r["id"]))
                n += 1
        return n

    # --- доказательства -------------------------------------------------

    def add_evidence(self, rid: str, ev: dict[str, Any], episode: str | None) -> None:
        cols = [c for c in EVIDENCE_FIELDS if ev.get(c) is not None]
        values = [int(ev[c]) if c in ("verbatim", "check_dirty") else ev[c] for c in cols]
        self.k.conn.execute(
            f"insert into evidence(record_id, episode, created_at, {','.join(cols)})"
            f" values(?,?,?,{','.join('?' for _ in cols)})",
            [rid, episode, now(), *values],
        )

    def evidence_for(self, rid: str) -> list[dict]:
        rows = self.k.conn.execute("select * from evidence where record_id=? order by id", (rid,)).fetchall()
        return [dict(r) for r in rows]

    def evidence_by_refs(self, refs: Iterable[str]) -> list[dict]:
        ids = list(dict.fromkeys(refs))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.k.conn.execute(f"select * from evidence where ref in ({marks})", ids).fetchall()
        return [dict(r) for r in rows]

    def evidence_by_ref(self, ref: str) -> list[dict]:
        return self.evidence_by_refs([ref])

    # --- граф: чтение через обе базы -----------------------------------------

    def get_node(self, nid: str) -> dict | None:
        if is_code_id(nid):
            return self.c.get_node(nid)
        if nid.startswith("ext:"):
            return self.c.get_node(nid) or self.k.get_node(nid)
        return self.k.get_node(nid)

    def upsert_knowledge_node(self, node: dict[str, Any]) -> None:
        self.k.upsert_node(node)

    def aliases(self, nid: str) -> list[str]:
        """Узел кода и все его прежние имена в ЭТОЙ рабочей копии (входящие RENAMED_TO)."""
        out, frontier = [nid], [nid]
        while frontier:
            cur = frontier.pop()
            for e in self.c.edges_where(dst=cur, rel="RENAMED_TO", active=1):
                if e["src"] not in out:
                    out.append(e["src"])
                    frontier.append(e["src"])
        return out

    def current_code_node(self, nid: str) -> dict | None:
        """По RENAMED_TO этой рабочей копии до живого узла: переименованная функция находится."""
        seen: set[str] = set()
        node = self.c.get_node(nid)
        while node and node["status"] == "moved" and node["id"] not in seen:
            seen.add(node["id"])
            nxt = [e["dst"] for e in self.c.edges_where(src=node["id"], rel="RENAMED_TO", active=1)]
            node = self.c.get_node(nxt[-1]) if nxt else None
        return node

    def edges_of(self, nid: str) -> list[dict]:
        """Все действующие рёбра узла в представлении этой рабочей копии.

        Смысловое ребро хранит id узла кода на момент записи. Если в этой копии функцию
        переименовали, ребро показывается на её текущем имени; в другой копии — на прежнем.
        Общая база при переименовании не меняется.
        """
        out: list[dict] = []
        if is_code_id(nid) or nid.startswith("ext:"):
            for e in self.c.edges_touching([nid]):
                out.append({**e, "db": "code"})
            names = self.aliases(nid) if is_code_id(nid) else [nid]
            for e in self.k.edges_touching(names):
                e = {**e, "db": "knowledge"}
                if e["src"] in names:
                    e["src"] = nid
                if e["dst"] in names:
                    e["dst"] = nid
                out.append(e)
            return out
        for e in self.k.edges_touching([nid]):
            e = {**e, "db": "knowledge"}
            for side in ("src", "dst"):
                if e[side] != nid and is_code_id(e[side]):
                    cur = self.current_code_node(e[side])
                    if cur:
                        e[side] = cur["id"]
            out.append(e)
        return out

    def edges_where(self, **where: Any) -> list[dict]:
        return ([{**e, "db": "code"} for e in self.c.edges_where(**where)]
                + [{**e, "db": "knowledge"} for e in self.k.edges_where(**where)])

    def degree(self, nid: str) -> int:
        return len(self.edges_of(nid))

    def add_edge(self, src: str, rel: str, dst: str, origin: str, **kw: Any) -> int:
        db = self.c if origin == "deterministic" else self.k
        return db.add_edge(src, rel, dst, origin, **kw)

    def end_edges(self, where: dict[str, Any], note: str, db: str = "knowledge") -> int:
        return (self.c if db == "code" else self.k).end_edges(where, note)

    def find_nodes_by_label(self, label: str) -> list[dict]:
        """Точное имя: квалифицированное (Class.method), короткое (method) или путь файла."""
        rows = self.c.conn.execute(
            "select * from nodes where status='present'"
            " and (label=? or label like ? escape '!' or (path=? and kind in ('file', 'doc')))",
            (label, "%." + label.replace("!", "!!").replace("%", "!%").replace("_", "!_"), label),
        ).fetchall()
        return [{k: r[k] for k in r.keys() if k != "n"} for r in rows]

    def nodes_in_file(self, path: str) -> list[dict]:
        rows = self.c.conn.execute("select * from nodes where path=?", (path,)).fetchall()
        return [{k: r[k] for k in r.keys() if k != "n"} for r in rows]

    def nodes_by_body(self, body_hash: str, status: str = "present") -> list[dict]:
        rows = self.c.conn.execute("select * from nodes where body_hash=? and status=?",
                                   (body_hash, status)).fetchall()
        return [{k: r[k] for k in r.keys() if k != "n"} for r in rows]

    def fts_nodes(self, query: str, limit: int) -> list[tuple[str, float]]:
        return self.c.fts_nodes(query, limit)

    def records_linked_to_file(self, path: str) -> list[tuple[str, str, str]]:
        """Записи, связанные с любой сущностью файла (ребром или доказательством): (запись, отношение, узел)."""
        out: list[tuple[str, str, str]] = []
        code_nodes = [n for n in self.nodes_in_file(path) if n["kind"] != "file"]
        for node in code_nodes:
            names = self.aliases(node["id"])
            for e in self.k.edges_touching(names):
                rid = e["src"] if e["dst"] in names else e["dst"]
                if self.k.conn.execute("select 1 from records where id=?", (rid,)).fetchone():
                    out.append((rid, e["rel"], node["id"]))
            for ev in self.evidence_by_refs(names):
                out.append((ev["record_id"], "EVIDENCE", node["id"]))
        for ev in self.evidence_by_refs([path]):
            out.append((ev["record_id"], "EVIDENCE", f"file:{path}"))
        return list(dict.fromkeys(out))

    # --- полнотекст и векторы -------------------------------------------

    def fts_records(self, query: str, limit: int) -> list[tuple[str, float]]:
        if not query:
            return []
        rows = self.k.conn.execute(
            "select record_id, bm25(records_fts) as s from records_fts where records_fts match ? order by s limit ?",
            (query, limit),
        ).fetchall()
        return [(r["record_id"], -float(r["s"])) for r in rows]

    def all_vectors(self) -> Iterable[tuple[str, bytes]]:
        for r in self.k.conn.execute("select record_id, vec from vectors").fetchall():
            yield r["record_id"], r["vec"]

    def get_vector(self, rid: str):
        from .textindex import from_blob

        row = self.k.conn.execute("select vec from vectors where record_id=?", (rid,)).fetchone()
        return from_blob(row["vec"]) if row else None

    # --- файлы кода и пометки (база рабочей копии) ------------------------

    def get_file(self, path: str) -> dict | None:
        row = self.c.conn.execute("select * from files where path=?", (path,)).fetchone()
        return dict(row) if row else None

    def put_file(self, path: str, sha: str, mtime: float, size: int) -> None:
        self.c.conn.execute(
            "insert into files(path, sha, mtime, size, indexed_at) values(?,?,?,?,?)"
            " on conflict(path) do update set sha=excluded.sha, mtime=excluded.mtime, size=excluded.size,"
            " indexed_at=excluded.indexed_at",
            (path, sha, mtime, size, now()),
        )

    def all_files(self) -> list[dict]:
        return [dict(r) for r in self.c.conn.execute("select * from files").fetchall()]

    def delete_file(self, path: str) -> None:
        self.c.conn.execute("delete from files where path=?", (path,))

    def mark_stale(self, rid: str, node_id: str, reason: str) -> bool:
        cur = self.c.conn.execute(
            "insert or ignore into stale_marks(record_id, node_id, reason, at) values(?,?,?,?)",
            (rid, node_id, reason, now()),
        )
        return cur.rowcount > 0

    def clear_stale(self, rid: str | None = None, node_id: str | None = None) -> int:
        cond, args = [], []
        if rid is not None:
            cond.append("record_id=?")
            args.append(rid)
        if node_id is not None:
            cond.append("node_id=?")
            args.append(node_id)
        if not cond:
            raise ValueError("clear_stale needs rid or node_id")
        return self.c.conn.execute(f"delete from stale_marks where {' and '.join(cond)}", args).rowcount

    def stale_reasons(self, rid: str) -> list[str]:
        rows = self.c.conn.execute("select reason from stale_marks where record_id=? order by at", (rid,))
        return [r[0] for r in rows]

    # --- эпизоды --------------------------------------------------------

    def insert_episode(self, eid: str, task: str, frame: dict, scope: str, owner: str | None) -> None:
        self.k.conn.execute(
            "insert into episodes(id, task, frame, status, started_at, scope, owner_token) values(?,?,?,?,?,?,?)",
            (eid, task, json.dumps(frame, ensure_ascii=False), "open", now(), scope, owner),
        )

    def get_episode(self, eid: str) -> dict | None:
        row = self.k.conn.execute("select * from episodes where id=?", (eid,)).fetchone()
        if not row:
            return None
        ep = dict(row)
        ep["frame"] = json.loads(ep["frame"])
        ep["injected"] = json.loads(ep["injected"])
        return ep

    def open_episode(self, scope: str) -> dict | None:
        row = self.k.conn.execute(
            "select id from episodes where status='open' and scope=? order by started_at desc, id desc limit 1",
            (scope,),
        ).fetchone()
        return self.get_episode(row["id"]) if row else None

    def last_closed_episode(self, scope: str) -> dict | None:
        row = self.k.conn.execute(
            "select id from episodes where status in ('committed', 'dropped') and scope=?"
            " order by ended_at desc, id desc limit 1",
            (scope,),
        ).fetchone()
        return self.get_episode(row["id"]) if row else None

    def update_episode(self, eid: str, **fields: Any) -> None:
        if "injected" in fields:
            fields["injected"] = json.dumps(fields["injected"])
        sets = ",".join(f"{k}=?" for k in fields)
        self.k.conn.execute(f"update episodes set {sets} where id=?", [*fields.values(), eid])

    def episodes_needing_export(self) -> list[dict]:
        rows = self.k.conn.execute(
            "select id from episodes where archive_json is not null and archive_exported_at is null"
        ).fetchall()
        return [self.get_episode(r["id"]) for r in rows]

    def add_note(self, eid: str, kind: str, text: str, ref: str | None) -> int:
        row = self.k.conn.execute(
            "select coalesce(max(seq), 0) + 1 from episode_notes where episode_id=?", (eid,)
        ).fetchone()
        seq = int(row[0])
        self.k.conn.execute(
            "insert into episode_notes(episode_id, seq, kind, text, ref, at) values(?,?,?,?,?,?)",
            (eid, seq, kind, text, ref, now()),
        )
        return seq

    def notes(self, eid: str) -> list[dict]:
        rows = self.k.conn.execute("select * from episode_notes where episode_id=? order by seq", (eid,))
        return [dict(r) for r in rows]

    def get_note(self, eid: str, seq: int) -> dict | None:
        row = self.k.conn.execute("select * from episode_notes where episode_id=? and seq=?", (eid, seq)).fetchone()
        if row:
            return dict(row)
        ep = self.get_episode(eid)  # эпизод закрыт — заметка живёт в каноническом архиве
        if ep and ep.get("archive_json"):
            for n in json.loads(ep["archive_json"]).get("notes", []):
                if n.get("seq") == seq:
                    return n
        return None

    def drop_notes(self, eid: str) -> int:
        return self.k.conn.execute("delete from episode_notes where episode_id=?", (eid,)).rowcount

    # --- применённые диффы ------------------------------------------------

    def applied_diff(self, diff_id: str) -> dict | None:
        row = self.k.conn.execute("select * from applied_diffs where diff_id=?", (diff_id,)).fetchone()
        return dict(row) if row else None

    def record_applied_diff(self, diff_id: str, content_hash: str, episode: str | None, result: dict) -> None:
        self.k.conn.execute(
            "insert into applied_diffs(diff_id, content_hash, episode, result, at) values(?,?,?,?,?)",
            (diff_id, content_hash, episode, json.dumps(result, ensure_ascii=False), now()),
        )

    # --- рабочее состояние --------------------------------------------------

    def get_working(self, scope: str) -> dict | None:
        row = self.k.conn.execute("select * from working_state where scope=?", (scope,)).fetchone()
        return dict(row) if row else None

    def put_working(self, scope: str, text: str) -> None:
        self.k.conn.execute(
            "insert into working_state(scope, text, updated_at) values(?,?,?)"
            " on conflict(scope) do update set text=excluded.text, updated_at=excluded.updated_at",
            (scope, text, now()),
        )

    def mark_working_synced(self, scope: str, digest: str) -> None:
        self.k.conn.execute("update working_state set file_synced_hash=? where scope=?", (digest, scope))

    # --- метрики --------------------------------------------------------

    def log_metric(self, event: str, data: dict, episode: str | None = None) -> None:
        self.k.conn.execute(
            "insert into metrics(at, episode_id, event, data) values(?,?,?,?)",
            (now(), episode, event, json.dumps(data, ensure_ascii=False)),
        )

    def metrics(self, event: str | None = None) -> list[dict]:
        if event:
            rows = self.k.conn.execute("select * from metrics where event=? order by id", (event,))
        else:
            rows = self.k.conn.execute("select * from metrics order by id")
        return [dict(r) | {"data": json.loads(r["data"])} for r in rows]
