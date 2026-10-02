"""Детерминированный граф кода по Python AST.

Связи CALLS / IMPORTS / DEFINED_IN / TESTED_BY берутся из синтаксического
дерева, а не у модели. Разрешение имён консервативное: если вызов нельзя
однозначно привязать к узлу в репозитории, ребро не создаётся. Лучше
пропустить связь, чем выдумать.

Индекс инкрементальный: файл перечитывается, только если изменились размер,
время или содержимое. Пропавшая функция ищется по отпечатку тела среди
появившихся в этом же прогоне: нашлась одна — это переименование или перенос
(RENAMED_TO), смысловые связи переезжают на новый узел. Не нашлась — узел
помечается missing, а записи памяти, которые на него ссылались, — stale.
"""

from __future__ import annotations

import ast
import hashlib
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from .store import SqliteStore, now
from .textindex import split_identifier

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".tox", ".mypy_cache", ".pytest_cache"}
STDLIB = set(getattr(sys, "stdlib_module_names", ()))
TRIVIAL_BODY = 10  # узлов AST в теле
# Рёбра, которыми владеет файл с источником ребра; они пересоздаются при перечитывании файла.
OWNED_BY_SRC = ("CALLS", "DEFINED_IN")


def file_node_id(rel: str) -> str:
    return f"file:{rel}"


def code_node_id(rel: str, qualname: str) -> str:
    return f"code:{rel}::{qualname}"


def is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")


def body_hash(node: ast.AST) -> str:
    """Отпечаток тела без имени и позиций: переименование его не меняет."""
    parts: list[str] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parts.append(ast.dump(node.args))
    elif isinstance(node, ast.ClassDef):
        parts.extend(ast.dump(b) for b in node.bases)
    parts.extend(ast.dump(stmt) for stmt in node.body)
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    # Тело из пары узлов (`pass`, `raise NotImplementedError`, `return x + 1`) встречается у многих
    # функций сразу. По такому отпечатку переименование не определяется — помечаем его префиксом.
    size = sum(1 for stmt in node.body for _ in ast.walk(stmt))
    return ("t:" if size < TRIVIAL_BODY else "") + digest


@dataclass
class Entity:
    qualname: str
    kind: str
    start: int
    end: int
    body_hash: str
    node: ast.AST
    class_name: str | None = None


@dataclass
class FileFacts:
    rel: str
    entities: list[Entity] = field(default_factory=list)
    # имя в модуле -> (относительный путь модуля, имя внутри него или None для модуля)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    imported_files: set[str] = field(default_factory=set)
    external: set[str] = field(default_factory=set)
    # id сущности -> (вид, цели вызовов). Считается сразу, дерево разбора не хранится.
    calls: dict[str, tuple[str, set[tuple[str, str]]]] = field(default_factory=dict)


class CodeIndexer:
    def __init__(self, store: SqliteStore, repo_root: Path, roots: tuple[str, ...] = ("app", "scripts", "tests"),
                 ignored=None):
        self.store = store
        self.root = Path(repo_root)
        self.roots = roots
        # ignored(paths) -> игнорируемые git пути: такие файлы не читаются и не хэшируются
        self.ignored = ignored or (lambda paths: [])

    # --- обход файлов ---------------------------------------------------

    def _collect(self, paths: list[str] | None) -> list[Path]:
        targets = paths or list(self.roots)
        files: list[Path] = []
        for t in targets:
            p = (self.root / t).resolve() if not Path(t).is_absolute() else Path(t)
            if p.is_file() and p.suffix == ".py":
                files.append(p)
            elif p.is_dir():
                for f in sorted(p.rglob("*.py")):
                    if not any(part in SKIP_DIRS for part in f.relative_to(p).parts):
                        files.append(f)
        root = self.root.resolve()
        rels = {f: f.resolve().relative_to(root).as_posix() for f in files if f.resolve().is_relative_to(root)}
        skip = set(self.ignored(sorted(rels.values())))
        return [f for f in files if rels.get(f) not in skip] if skip else files

    @staticmethod
    def file_sha(p: Path) -> str:
        return hashlib.sha1(p.read_bytes()).hexdigest()

    def changed_files(self) -> dict:
        """Что изменилось на диске с последнего индекса: правки без коммита тоже. Только чтение."""
        files = self._collect(None)
        known = {f["path"]: f for f in self.store.all_files()}
        seen, changed, new = set(), [], []
        for f in files:
            rel = self.rel(f)
            seen.add(rel)
            k = known.get(rel)
            if not k:
                new.append(rel)
                continue
            st = f.stat()
            # Как index(): свежий файл (правка в тот же тик времени того же размера) сверяем по хэшу.
            recent = time.time() - st.st_mtime < 2.0
            if (k["mtime"], k["size"]) == (st.st_mtime, st.st_size) and not recent:
                continue
            if self.file_sha(f) != k["sha"]:
                changed.append(rel)
        deleted = [p for p in known if p not in seen and not (self.root / p).exists()]
        return {"changed": changed[:20], "new": new[:20], "deleted": deleted[:20],
                "total": len(changed) + len(new) + len(deleted)}

    def disk_state(self) -> dict[str, tuple]:
        """Снимок файлов индексируемых корней: (mtime_ns, размер, хэш только у свежих файлов).
        Нужен, чтобы заметить параллельную правку между обновлением карты и записью."""
        out: dict[str, tuple] = {}
        cutoff = time.time() - 2.0
        for f in self._collect(None):
            st = f.stat()
            out[self.rel(f)] = (st.st_mtime_ns, st.st_size,
                                self.file_sha(f) if st.st_mtime >= cutoff else None)
        return out

    def changed_since(self, snapshot: dict[str, tuple]) -> list[str]:
        """Файлы, которые появились, исчезли или изменились после снимка disk_state()."""
        now_state = {self.rel(f): f for f in self._collect(None)}
        diff = sorted(set(now_state) ^ set(snapshot))
        for rel, f in now_state.items():
            if rel not in snapshot:
                continue
            mtime_ns, size, sha = snapshot[rel]
            st = f.stat()
            if (st.st_mtime_ns, st.st_size) != (mtime_ns, size) or (sha is not None and self.file_sha(f) != sha):
                diff.append(rel)
        return sorted(set(diff))

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    def _module_to_rel(self, dotted: str) -> str | None:
        base = dotted.replace(".", "/")
        for cand in (f"{base}.py", f"{base}/__init__.py"):
            if (self.root / cand).is_file():
                return cand
        return None

    # --- разбор одного файла --------------------------------------------

    def _parse(self, rel: str, source: str) -> FileFacts | None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return None
        facts = FileFacts(rel=rel)
        pkg_parts = rel.split("/")[:-1]

        for stmt in ast.walk(tree):
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    target = self._module_to_rel(alias.name)
                    if target:
                        facts.imports[alias.asname or alias.name] = (target, None)
                        facts.imported_files.add(target)
                    else:
                        top = alias.name.split(".")[0]
                        if top not in STDLIB:
                            facts.external.add(top)
            elif isinstance(stmt, ast.ImportFrom):
                if stmt.level:
                    base = pkg_parts[: len(pkg_parts) - (stmt.level - 1)] if stmt.level > 1 else pkg_parts
                    dotted = ".".join([*base, *(stmt.module.split(".") if stmt.module else [])])
                else:
                    dotted = stmt.module or ""
                mod_rel = self._module_to_rel(dotted) if dotted else None
                if not mod_rel:
                    top = dotted.split(".")[0] if dotted else ""
                    if top and top not in STDLIB and not stmt.level:
                        facts.external.add(top)
                    continue
                facts.imported_files.add(mod_rel)
                for alias in stmt.names:
                    sub = self._module_to_rel(f"{dotted}.{alias.name}")
                    if sub:
                        facts.imports[alias.asname or alias.name] = (sub, None)
                        facts.imported_files.add(sub)
                    else:
                        facts.imports[alias.asname or alias.name] = (mod_rel, alias.name)

        def visit(body: list[ast.stmt], prefix: str, class_name: str | None) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    qual = f"{prefix}{node.name}"
                    if isinstance(node, ast.ClassDef):
                        kind = "class"
                    elif class_name:
                        kind = "method"
                    elif node.name.startswith("test") and is_test_file(rel):
                        kind = "test"
                    else:
                        kind = "function"
                    end = getattr(node, "end_lineno", node.lineno) or node.lineno
                    start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
                    facts.entities.append(Entity(qual, kind, start, end, body_hash(node), node, class_name))
                    if isinstance(node, ast.ClassDef):
                        visit(node.body, qual + ".", qual)
                    else:
                        visit(node.body, qual + ".", class_name)

        visit(tree.body, "", None)
        return facts

    def _calls(self, facts: FileFacts, ent: Entity) -> set[tuple[str, str]]:
        """Вызовы внутри сущности -> множество (путь модуля, квалифицированное имя)."""
        local_top = {e.qualname for e in facts.entities if "." not in e.qualname}
        local_classes = {e.qualname for e in facts.entities if e.kind == "class" and "." not in e.qualname}
        out: set[tuple[str, str]] = set()
        body = ent.node.body if not isinstance(ent.node, ast.ClassDef) else []

        # Тип локальной переменной знаем только в одном случае: `x = Класс(...)` в этой же функции,
        # и Класс разрешается в репозиторий. Переприсваивание другим значением снимает тип.
        var_class: dict[str, tuple[str, str] | None] = {}
        for stmt in body:
            for node in ast.walk(stmt):
                if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)):
                    continue
                name, val = node.targets[0].id, node.value
                cls = None
                if isinstance(val, ast.Call) and isinstance(val.func, ast.Name):
                    if val.func.id in local_classes:
                        cls = (facts.rel, val.func.id)
                    elif val.func.id in facts.imports and facts.imports[val.func.id][1]:
                        mod, imported = facts.imports[val.func.id]
                        if imported[:1].isupper():
                            cls = (mod, imported)
                if name in var_class and var_class[name] != cls:
                    var_class[name] = None
                else:
                    var_class[name] = cls

        for stmt in body:
            for node in ast.walk(stmt):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Name):
                    if fn.id in local_top:
                        out.add((facts.rel, fn.id))
                    elif fn.id in facts.imports:
                        mod, name = facts.imports[fn.id]
                        if mod and name:
                            out.add((mod, name))
                elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                    owner, attr = fn.value.id, fn.attr
                    if owner in ("self", "cls") and ent.class_name:
                        out.add((facts.rel, f"{ent.class_name}.{attr}"))
                    elif var_class.get(owner):
                        mod, cls = var_class[owner]
                        out.add((mod, f"{cls}.{attr}"))
                    elif owner in local_top:
                        out.add((facts.rel, f"{owner}.{attr}"))
                    elif owner in facts.imports:
                        mod, name = facts.imports[owner]
                        if mod and name is None:
                            out.add((mod, attr))
                        elif mod and name:
                            out.add((mod, f"{name}.{attr}"))
        return out

    # --- индексация -----------------------------------------------------

    def index(self, paths: list[str] | None = None, progress: bool = False, force: bool = False) -> dict:
        started = time.monotonic()
        files = self._collect(paths)
        report: dict = {"files_seen": len(files), "files_parsed": 0, "nodes": 0, "edges": 0,
                        "moved": [], "missing": [], "changed": [], "stale_records": [], "parse_errors": [],
                        "restored": 0}
        seen_rel: set[str] = set()
        parsed: list[FileFacts] = []
        new_ids: set[str] = set()
        disappeared: list[dict] = []
        changed_bodies: list[tuple[str, str]] = []

        with self.store.code_transaction():
            for i, f in enumerate(files, 1):
                if progress and (i % 200 == 0 or i == len(files)):
                    el = time.monotonic() - started
                    eta = el / i * (len(files) - i)
                    print(f"[index] {i}/{len(files)} {100 * i // len(files)}% elapsed {el:.1f}s eta {eta:.1f}s",
                          file=sys.stderr, flush=True)
                rel = self.rel(f)
                seen_rel.add(rel)
                st = f.stat()
                known = self.store.get_file(rel)
                # Правка того же размера в тот же тик времени не меняет ни размер, ни mtime.
                # Как git с «racily clean» файлами: свежий файл всегда сверяем по хэшу.
                recent = time.time() - st.st_mtime < 2.0
                if (not force and not recent and known and known["mtime"] == st.st_mtime
                        and known["size"] == st.st_size):
                    continue
                raw = f.read_bytes()
                sha = hashlib.sha1(raw).hexdigest()
                if not force and known and known["sha"] == sha:
                    self.store.put_file(rel, sha, st.st_mtime, st.st_size)
                    continue
                facts = self._parse(rel, raw.decode("utf-8", errors="replace"))
                self.store.put_file(rel, sha, st.st_mtime, st.st_size)
                if facts is None:
                    # Файл не разбирается (правка на середине). Узлы остаются в прежнем виде,
                    # хэш не запоминаем, чтобы следующий прогон перечитал файл.
                    report["parse_errors"].append(rel)
                    self.store.delete_file(rel)
                    continue
                report["files_parsed"] += 1
                old = {n["id"]: n for n in self.store.nodes_in_file(rel) if n["kind"] != "file"}
                for nid in old:
                    self._close_owned(nid, "reindexed")
                self.store.end_edges({"src": file_node_id(rel), "rel": "IMPORTS", "origin": "deterministic"},
                                     "reindexed", db="code")
                self.store.c.upsert_node({
                    "id": file_node_id(rel), "kind": "file", "label": rel, "path": rel,
                    "terms": " ".join(split_identifier(rel)), "status": "present",
                })
                current: set[str] = set()
                for ent in facts.entities:
                    nid = code_node_id(rel, ent.qualname)
                    current.add(nid)
                    prev = old.get(nid)
                    if prev is None or prev["status"] != "present":
                        new_ids.add(nid)
                        if prev is not None:  # сущность вернулась (например, переключили ветку)
                            report["restored"] += self.store.clear_stale(node_id=nid)
                    elif prev["body_hash"] != ent.body_hash:
                        changed_bodies.append((nid, ent.body_hash))
                    self.store.c.upsert_node({
                        "id": nid, "kind": ent.kind, "label": ent.qualname, "path": rel,
                        "start_line": ent.start, "end_line": ent.end, "body_hash": ent.body_hash,
                        "terms": " ".join(split_identifier(ent.qualname) + split_identifier(rel)),
                        "status": "present",
                    })
                    report["nodes"] += 1
                for ent in facts.entities:
                    facts.calls[code_node_id(rel, ent.qualname)] = (ent.kind, self._calls(facts, ent))
                facts.entities = []  # дерево разбора больше не нужно: на большом репо оно съедает гигабайт
                disappeared.extend(n for nid, n in old.items() if nid not in current and n["status"] == "present")
                parsed.append(facts)

            # Удалённые файлы внутри проиндексированных корней.
            scope = [Path(p).as_posix().rstrip("/") for p in (paths or list(self.roots))]
            for known in self.store.all_files():
                rel = known["path"]
                in_scope = any(rel == s or rel.startswith(s + "/") for s in scope)
                if in_scope and rel not in seen_rel and not (self.root / rel).exists():
                    for n in self.store.nodes_in_file(rel):
                        if n["kind"] == "file":
                            self.store.c.set_node_status(n["id"], "missing")
                        elif n["status"] == "present":
                            disappeared.append(n)
                        self._close_owned(n["id"], "file deleted")
                        self.store.end_edges({"src": n["id"], "rel": "IMPORTS", "origin": "deterministic"},
                                             "file deleted", db="code")
                    self.store.delete_file(rel)

            # Связи строим после всех файлов: цель вызова может лежать в файле, разобранном позже.
            for facts in parsed:
                fid = file_node_id(facts.rel)
                for target in sorted(facts.imported_files):
                    if self.store.get_node(file_node_id(target)) is None:
                        self.store.c.upsert_node({"id": file_node_id(target), "kind": "file", "label": target,
                                                "path": target, "terms": " ".join(split_identifier(target)),
                                                "status": "present" if (self.root / target).exists() else "missing"})
                    self.store.add_edge(fid, "IMPORTS", file_node_id(target), "deterministic")
                    report["edges"] += 1
                for ext in sorted(facts.external):
                    self.store.c.upsert_node({"id": f"ext:{ext}", "kind": "external", "label": ext,
                                            "terms": " ".join(split_identifier(ext)), "status": "present"})
                    self.store.add_edge(fid, "IMPORTS", f"ext:{ext}", "deterministic")
                    report["edges"] += 1
                for nid, (kind, calls) in facts.calls.items():
                    self.store.add_edge(nid, "DEFINED_IN", fid, "deterministic")
                    report["edges"] += 1
                    for mod, qual in sorted(calls):
                        target = code_node_id(mod, qual)
                        tnode = self.store.get_node(target)
                        if not tnode or tnode["status"] != "present" or target == nid:
                            continue
                        self.store.add_edge(nid, "CALLS", target, "deterministic")
                        report["edges"] += 1
                        if kind == "test":
                            self.store.add_edge(target, "TESTED_BY", nid, "deterministic")
                            report["edges"] += 1

            self._handle_disappeared(disappeared, new_ids, report)
            self._handle_changed(changed_bodies, report)

        report["elapsed_s"] = round(time.monotonic() - started, 3)
        with self.store.transaction():
            self.store.log_metric("index", {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()})
        return report

    def _close_owned(self, nid: str, note: str) -> None:
        """Закрыть рёбра, которые пересоздаёт разбор файла этой сущности.

        CALLS и DEFINED_IN принадлежат файлу вызывающего. TESTED_BY (реализация -> тест)
        принадлежит файлу теста: правка реализации его не трогает. RENAMED_TO не закрывается
        никогда — это история.
        """
        for rel in OWNED_BY_SRC:
            self.store.end_edges({"src": nid, "rel": rel, "origin": "deterministic"}, note, db="code")
        self.store.end_edges({"dst": nid, "rel": "TESTED_BY", "origin": "deterministic"}, note, db="code")

    def aliases(self, nid: str) -> list[str]:
        return self.store.aliases(nid)

    def _handle_disappeared(self, disappeared: list[dict], new_ids: set[str], report: dict) -> None:
        for old in disappeared:
            self.store.end_edges({"src": old["id"], "rel": "TESTED_BY", "origin": "deterministic"}, "entity gone",
                                 db="code")
            bh = old["body_hash"] or ""
            cands = [n for n in self.store.nodes_by_body(bh) if n["id"] in new_ids
                     and n["kind"] == old["kind"]] if bh and not bh.startswith("t:") else []
            if len(cands) == 1:
                new = cands[0]
                self.store.c.set_node_status(old["id"], "moved")
                self.store.add_edge(old["id"], "RENAMED_TO", new["id"], "deterministic",
                                    note="same body hash after reindex")
                # Общие смысловые рёбра не трогаем: в этой копии их покажет edges_of() на новом имени,
                # в копии другой ветки — на прежнем. Переименование не становится истиной для всех веток.
                report["moved"].append({"from": old["id"], "to": new["id"]})
            else:
                self.store.c.set_node_status(old["id"], "missing")
                report["missing"].append(old["id"])
                reason = f"code entity {old['id']} disappeared at reindex {now()}"
                for rid in self._records_touching(old["id"]):
                    self._mark_stale(rid, old["id"], reason, report)

    def _handle_changed(self, changed: list[tuple[str, str]], report: dict) -> None:
        for nid, new_hash in changed:
            report["changed"].append(nid)
            for alias in self.aliases(nid):
                for ev in self.store.evidence_by_ref(alias):
                    if ev["kind"] != "code" or not ev["excerpt_hash"]:
                        continue
                    if ev["excerpt_hash"] == new_hash:  # тело вернулось к записанному
                        report["restored"] += self.store.clear_stale(rid=ev["record_id"], node_id=nid)
                    else:
                        self._mark_stale(ev["record_id"], nid, f"evidence {nid} changed since it was recorded",
                                         report)

    def _records_touching(self, nid: str) -> list[str]:
        names = self.aliases(nid)
        out = []
        for e in self.store.k.edges_touching(names):
            other = e["dst"] if e["src"] in names else e["src"]
            if e["origin"] != "deterministic" and self.store.get_record(other):
                out.append(other)
        out.extend(ev["record_id"] for ev in self.store.evidence_by_refs(names))
        return sorted(set(out))

    def _mark_stale(self, rid: str, node_id: str, reason: str, report: dict) -> None:
        # Пометка живёт в базе этой рабочей копии и касается только её кода.
        if self.store.get_record(rid) and self.store.mark_stale(rid, node_id, reason):
            report["stale_records"].append(rid)


def inside_root(root: Path, rel: str) -> Path | None:
    """Путь из доказательства обязан остаться внутри репозитория: `../secret` и `/etc/x` — нет."""
    base = Path(root).resolve()
    p = (base / rel).resolve()
    return p if p.is_relative_to(base) else None


def read_lines(root: Path, rel: str, start: int | None, end: int | None,
               max_lines: int | None = 80) -> str | None:
    p = inside_root(root, rel)
    if p is None or not p.is_file():
        return None
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    s = max(1, start or 1)
    e = min(len(lines), end or len(lines))
    if max_lines is not None and e - s + 1 > max_lines:
        e = s + max_lines - 1
    return "\n".join(lines[s - 1 : e])


def excerpt_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()
