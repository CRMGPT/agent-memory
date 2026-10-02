"""Долговременная память: записи, доказательства, связи, правила достоверности.

Публичный API (memory.search / get / related / sources / add / update / invalidate /
link / commit) описан в docs/design.md. Главные правила:

* факт, решение, ограничение без доказательства не принимаются;
* «источник найден», «фрагмент совпадает» и «утверждение проверено» — три разных вопроса;
  verified даёт только проверка, которую выполнила сама память (checks.py);
* слово владельца и вывод тула — засвидетельствованное, веб-страница — внешнее: ни то,
  ни другое не становится проверенным;
* формулировку не правят на месте: SUPERSEDE, старая версия остаётся в истории;
* перемены статуса действуют там, где их коммит входит в историю рабочей копии (view.py);
* тот же subject с другим смыслом — ConflictError, а не тихая перезапись.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from . import checks
from . import schema as S
from .codegraph import CodeIndexer, excerpt_hash, inside_root, read_lines
from .paths import default_scope, git_base
from .store import SqliteStore, is_code_id, now
from .textindex import HashingEmbedder, cosine, normalize_statement, split_identifier, to_blob
from .view import APPLIES, CodeView

_LOCATOR = re.compile(r"^L?(\d+)(?:\s*-\s*L?(\d+))?$")
_RECORD_ID = re.compile(r"^(" + "|".join(S.ID_PREFIX.values()) + r")_\d{5}$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_NOTE_REF = re.compile(r"^note:(episode_\d{6})#(\d+)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
NEAR_DUP = 0.92
MAX_STATEMENT = 600


def commit_exists(root: Path, ref: str) -> bool:
    """Коммит-ссылка проверяется в git, а не принимается на слово."""
    if not _SHA.match(ref):
        return False
    base = git_base(Path(root))
    if not base:
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        out = subprocess.run([*base, "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True, timeout=10,
                             env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


def parse_locator(loc: str | None) -> tuple[int, int] | None:
    if not loc:
        return None
    m = _LOCATOR.match(loc.strip())
    if not m:
        return None
    a = int(m.group(1))
    return a, int(m.group(2) or a)


def _inside(path: Path, root: Path) -> tuple[str, ...]:
    """Каталог памяти внутри рабочей копии — путь относительно корня (его файлы не код)."""
    try:
        return (Path(path).resolve().relative_to(Path(root).resolve()).as_posix(),)
    except ValueError:
        return ()


_SECRET_PATTERNS = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b(?:sk|rk)-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.",
    r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?[^\s'\"]{6,}",
)]


def _strings(value, depth: int = 0):
    if depth > 12:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k, depth + 1)
            yield from _strings(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, depth + 1)


def refuse_secrets(*values, what: str = "the text") -> None:
    """Память не хранит секреты. Решение одно для всех путей записи — ОТКАЗ, а не скрытая правка:
    текст, похожий на ключ, токен или пароль (в записи, диффе, заметке любого вида, тексте задачи,
    рабочем состоянии, итоге, причине), не сохраняется вовсе, ничего не записывается. Проверяются
    все строки вложенных структур. Уже сохранённое прежними версиями автоматически не переписывается."""
    for value in values:
        for text in _strings(value):
            if any(p.search(text) for p in _SECRET_PATTERNS):
                raise S.ValidationError(f"{what} looks like it contains a secret (key, token or password): memory "
                                        "does not store secrets, nothing was saved. Describe where the secret is "
                                        "configured instead of pasting it")


class Memory:
    def __init__(self, store_dir: Path, repo_root: Path, roots: tuple[str, ...] = ("app", "scripts", "tests"),
                 embedder: Any = None, scope: str | None = None, allow_old: bool = False,
                 read_only: bool = False, timeout: float = 30.0):
        """read_only=True — тот же механизм оценки (применимость, статус, достоверность, источники),
        но без единой записи: база открыта только на чтение, каталоги не создаются. Им пользуется
        автоматическая подгрузка памяти (reader.py); писать через такой объект нельзя."""
        self.dir = Path(store_dir)
        self.read_only = read_only
        if not read_only:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "episodes").mkdir(exist_ok=True)
            (self.dir / "working").mkdir(exist_ok=True)
        self.root = Path(repo_root)
        self.scope = scope or os.environ.get("AGENT_MEMORY_SCOPE") or default_scope(self.root)
        self.store = SqliteStore(self.dir, self.scope, allow_old=allow_old, read_only=read_only, timeout=timeout)
        self.view = CodeView(self.root, self.scope, exclude=_inside(self.dir, self.root))
        self.embedder = embedder or HashingEmbedder()
        self.code = CodeIndexer(self.store, self.root, roots, ignored=self.view.ignored)

    def close(self) -> None:
        self.store.close()

    # ======================================================= применимость

    def status(self, rec: dict, transitions: list[dict] | None = None) -> str:
        """Статус записи в этой рабочей копии."""
        if transitions is None:
            transitions = self.store.transitions([rec["id"]]).get(rec["id"], [])
        return self.view.effective_status(rec, transitions)

    def applicability(self, rec: dict) -> str:
        return self.view.classify_record(rec)

    def is_current(self, rec: dict | None, transitions: list[dict] | None = None) -> bool:
        """Запись действует здесь: сделана в нашей истории (или в этой копии) и не закрыта здесь."""
        return bool(rec) and self.applicability(rec) in APPLIES and self.status(rec, transitions) == "active"

    def certainty(self, rec: dict, status: str | None = None) -> str:
        """Подкреплённость сейчас: из статуса, проверок и записанного уровня."""
        status = status or self.status(rec)
        if status == "superseded":
            return "superseded"
        if status == "invalidated":
            return "contradicted"
        level = checks.best_level(self, self.store.evidence_for(rec["id"]))
        if level:  # есть проверки: уровень — по их свежему состоянию, а не по записанной метке
            return level
        stored = rec["certainty"]
        return "sourced" if stored in ("verified", "check_passed", "needs_recheck") else stored

    def describe(self, rec: dict) -> dict:
        """Запись глазами этой рабочей копии: статус, применимость, подкреплённость."""
        tr = self.store.transitions([rec["id"]]).get(rec["id"], [])
        status = self.status(rec, tr)
        cls = self.applicability(rec)
        return {"status_here": status, "applicability": cls, "applies_here": cls in APPLIES,
                "certainty_here": self.certainty(rec, status), "scope_note": self.view.describe(cls, rec)}

    def anchor_pending(self) -> int:
        """Работа закоммичена — записи этой копии на незакоммиченных правках привязать к коммиту."""
        self.view.refresh()  # дёшево: только сброс кэша, git спрашивается лениво
        if not self.store.has_pending(self.scope):
            return 0
        head = self.view.head
        if not head or self.view.dirty:
            return 0
        with self.store.transaction():
            return self.store.anchor_pending(self.scope, self.view.can_anchor, head)

    # ================================================================ чтение

    def get(self, rid: str) -> dict:
        self.view.refresh()  # публичное чтение — по текущему состоянию кода копии
        rec = self.store.get_record(rid)
        if not rec:
            raise S.NotFound(rid)
        rec["evidence"] = self.store.evidence_for(rid)
        rec["edges"] = self.store.edges_of(rid)
        rec["transitions"] = self.store.transitions([rid]).get(rid, [])
        rec.update(self.describe(rec))
        return rec

    def search(self, query: str, limit: int = 10, include_history: bool = False,
               sections: tuple[str, ...] | None = None) -> list[dict]:
        from .retrieval import Retriever

        self.view.refresh()
        return Retriever(self, sections=sections).direct(query, limit=limit, include_history=include_history)

    def related(self, nid: str, depth: int = 1, limit: int = 30) -> list[dict]:
        """Окрестность узла: ограниченный BFS, а не вся компонента связности."""
        out: list[dict] = []
        frontier = [nid]
        seen = {nid}
        for hop in range(1, max(1, min(depth, 3)) + 1):
            nxt = []
            for cur in frontier:
                for e in self.store.edges_of(cur):
                    other = e["dst"] if e["src"] == cur else e["src"]
                    if other in seen:
                        continue
                    seen.add(other)
                    node = self.store.get_node(other) or {"id": other, "kind": "unknown", "label": other}
                    direction = "out" if e["src"] == cur else "in"
                    out.append({"hop": hop, "via": cur, "rel": e["rel"], "direction": direction,
                                "origin": e["origin"], "node": node})
                    nxt.append(other)
                    if len(out) >= limit:
                        return out
            frontier = nxt
        return out

    def current_code_node(self, nid: str) -> dict | None:
        return self.store.current_code_node(nid)

    def code_freshness(self) -> dict:
        """Совпадает ли индекс кода этой рабочей копии с файлами на диске (правки без коммита тоже)."""
        changed = self.code.changed_files()
        return {"fresh": not changed["total"], **changed,
                "indexed_head": self.store.c.meta("indexed_head"), "current_head": self.view.head}

    def _file_changed_since_index(self, rel: str) -> bool:
        known = self.store.get_file(rel)
        p = inside_root(self.root, rel)
        if not known or p is None or not p.is_file():
            return True
        st = p.stat()
        if known["mtime"] == st.st_mtime and known["size"] == st.st_size:
            return False
        return self.code.file_sha(p) != known["sha"]

    def sources(self, rid: str, max_lines: int = 60) -> list[dict]:
        self.view.refresh()  # публичное чтение — по текущему состоянию кода копии
        return self._sources(rid, max_lines)

    def _sources(self, rid: str, max_lines: int = 60) -> list[dict]:
        """Доказательства записи. Три отдельных ответа:

        available — источник найден и доступен;
        match     — сохранённый фрагмент совпадает с текущим (unchanged / changed / not_recorded);
        check     — для проверок: прошла ли и не изменилась ли её цель.
        verification — общий итог одним словом, для краткого вывода. Для проверок: check_ok
                       (прошла и свежая), check_stale (прошла, но устарела), check_failed — это
                       НЕ уровни подкреплённости: уровень (verified / check_passed) — в check.level.
        """
        out = []
        for ev in self.store.evidence_for(rid):
            item = {k: ev[k] for k in ("kind", "category", "ref", "locator", "note", "episode")}
            item.update(available=None, match=None, verification="not_applicable")
            kind = ev["kind"]
            if kind in ("file", "doc", "test") and not ev["ref"].startswith("code:"):
                rng = parse_locator(ev["locator"])
                full = read_lines(self.root, ev["ref"], *(rng or (None, None)), max_lines=None)
                if full is None:
                    item.update(available=False, verification="missing")
                else:
                    item["available"] = True
                    item["excerpt"] = "\n".join(full.splitlines()[:max_lines])
                    if ev["excerpt_hash"]:
                        item["match"] = "unchanged" if excerpt_hash(full) == ev["excerpt_hash"] else "changed"
                    else:
                        item["match"] = "not_recorded"
                    item["verification"] = item["match"] if item["match"] != "not_recorded" else "present"
                if kind == "test":
                    item["label"] = "test file reference: the test was NOT run for this claim"
            elif kind == "code":
                node = self.current_code_node(ev["ref"])
                if not node or node["status"] != "present":
                    item.update(available=False, verification="missing")
                elif self._file_changed_since_index(node["path"]):
                    item.update(available=True, verification="needs_reindex",
                                label="file changed since this worktree was indexed: run `index`")
                else:
                    item["available"] = True
                    item["current_lines"] = f"{node['path']}:L{node['start_line']}-L{node['end_line']}"
                    if node["id"] != ev["ref"]:
                        item["current_ref"] = node["id"]
                    item["excerpt"] = read_lines(self.root, node["path"], node["start_line"], node["end_line"],
                                                 max_lines=max_lines)
                    if ev["excerpt_hash"]:
                        item["match"] = "unchanged" if ev["excerpt_hash"] == node["body_hash"] else "changed"
                    item["verification"] = item["match"] or "present"
            elif kind == "commit":
                ok = commit_exists(self.root, ev["ref"])
                item.update(available=ok, verification="present" if ok else "missing")
            elif kind == "check":
                st = checks.state(self, ev)
                item["check"] = st
                item["available"] = st["target"] != "missing"
                item["verification"] = ("check_ok" if st["passed_and_fresh"]
                                        else "check_stale" if st["satisfied"] else "check_failed")
                item["label"] = (f"{st['method']} check: {st['condition']} -> {st['result']}, target {st['target']}"
                                 f", at {(st['commit'] or 'no-git')[:10]}{' +uncommitted' if st['dirty'] else ''}")
            elif kind in ("user", "tool"):
                how = "linked to the original output" if ev.get("verbatim") else "paraphrased by the model"
                item["label"] = f"attested by {kind} ({how}); not independently checked"
                if ev.get("excerpt"):
                    item["excerpt"] = ev["excerpt"][:2000]
            elif kind == "url":
                item["label"] = (f"external page, accessed {ev.get('accessed_at')}"
                                 + (f", version {ev['version_label']}" if ev.get("version_label") else "")
                                 + "; supports what the page says, not that our code works")
                item["excerpt"] = ev.get("excerpt")
            out.append(item)
        return out

    def why(self, rid: str) -> dict:
        """Ответ на «почему мы так считаем»: доказательства, эпизод, история, смежные утверждения."""
        rec = self.get(rid)
        ep = self.store.get_episode(rec["created_from"]) if rec.get("created_from") else None
        chain = []
        cur = rec
        while cur and cur.get("superseded_by"):
            cur = self.store.get_record(cur["superseded_by"])
            if cur:
                chain.append({"id": cur["id"], "statement": cur["statement"]})
        predecessors = [e["dst"] for e in rec["edges"] if e["rel"] == "SUPERSEDES" and e["src"] == rid]
        support = [e for e in rec["edges"] if e["rel"] in ("SUPPORTED_BY", "CONTRADICTS", "CAUSED_BY", "RESOLVES")]
        return {
            "record": {k: rec[k] for k in ("id", "type", "statement", "reason", "certainty", "confidence",
                                          "subject", "version", "valid_from", "stale_reason", "observed_commit",
                                          "observed_branch", "observed_scope", "observed_dirty",
                                          "legacy_certainty", "status_here", "certainty_here",
                                          "applicability", "scope_note")},
            "sources": self.sources(rid),
            "transitions": rec["transitions"],
            "created_in_episode": {"id": ep["id"], "task": ep["task"], "summary": ep.get("summary"),
                                   "archive": ep.get("archive_path")} if ep else None,
            "versions": [{k: v[k] for k in ("version", "change", "episode", "at")}
                         for v in self.store.record_versions(rid)],
            "superseded_by_chain": chain,
            "supersedes": predecessors,
            "support_and_conflict": support,
        }

    def history(self, subject: str) -> list[dict]:
        return [r | self.describe(r) for r in self.store.records_by_subject(subject)]

    # ============================================================ проверки

    def _norm_evidence(self, evs: list[dict] | None, episode: str | None) -> list[dict]:
        out = []
        for raw in evs or []:
            if isinstance(raw, checks.Prepared):  # проверка уже выполнена этим процессом до транзакции
                out.append(dict(raw))
                continue
            if isinstance(raw, str):
                kind, _, ref = raw.partition(":")
                raw = {"kind": kind.strip(), "ref": ref.strip()}
            ev = dict(raw)
            kind = ev.get("kind")
            ref = (ev.get("ref") or "").strip()
            if kind not in S.EVIDENCE_KINDS:
                raise S.ValidationError(f"evidence kind {kind!r} unknown; use one of {S.EVIDENCE_KINDS}")
            if kind == "episode" and not ref:
                ref = episode or ""
            if not ref and kind != "model":
                raise S.ValidationError(f"evidence of kind {kind} needs ref (who said it, which run, which file)")
            norm: dict = {"kind": kind, "ref": ref, "category": S.EVIDENCE_CATEGORY[kind],
                          "note": ev.get("note"), "locator": ev.get("locator")}
            if kind == "check":
                out.append(checks.run(self, ev))
                continue
            if kind in ("user", "tool"):
                m = _NOTE_REF.match(ref)
                if m:  # ссылка на исходный вывод, записанный в эпизод: копируем его дословно
                    note = self.store.get_note(m.group(1), int(m.group(2)))
                    if not note:
                        raise S.ValidationError(f"{ref} does not exist")
                    norm.update(excerpt=note["text"], verbatim=1)
                    norm["note"] = norm["note"] or note["text"][:300]
                elif not (ev.get("note") or "").strip():
                    raise S.ValidationError(
                        f"{kind} evidence needs note (what exactly was said or printed), or a ref "
                        "note:<episode>#<seq> to an episode note with the original output")
                else:
                    norm["verbatim"] = 0  # пересказ модели остаётся пересказом
            elif kind == "url":
                quote = (ev.get("quote") or ev.get("note") or "").strip()
                accessed = (ev.get("accessed") or "").strip()
                if not re.match(r"^https?://", ref):
                    raise S.ValidationError("url evidence ref must be an http(s) address")
                if not quote or not _DATE.match(accessed):
                    raise S.ValidationError("url evidence needs quote (the exact fragment) and accessed "
                                            "(YYYY-MM-DD); a bare URL supports nothing")
                norm.update(excerpt=quote, accessed_at=accessed, version_label=ev.get("version"))
            elif kind == "episode":
                if not self.store.get_episode(ref):
                    raise S.ValidationError(f"episode evidence {ref!r} does not exist")
            elif kind == "commit":
                if not commit_exists(self.root, ref):
                    raise S.ValidationError(f"commit {ref!r} not found in git at {self.root}")
            elif kind == "code" or ref.startswith("code:"):
                node_id = self.resolve_target(ref, allow_create=False)
                node = self.store.get_node(node_id)
                if not node or not is_code_id(node["id"]) or node["id"].startswith("file:"):
                    raise S.ValidationError(f"code evidence {ref!r} must point to an indexed code entity")
                self.refuse_ignored([self._node_path(node_id)], "code evidence in")
                norm.update(kind="code", category="verifiable", ref=node_id,
                            locator=f"L{node['start_line']}-L{node['end_line']}", excerpt_hash=node["body_hash"])
            elif kind in ("file", "doc", "test"):
                ref = ref.removeprefix("file:")
                path = inside_root(self.root, ref)
                if path is None or not path.is_file():
                    raise S.ValidationError(f"evidence file {ref!r} not found under repo root {self.root}")
                self.refuse_ignored([ref], "evidence file")
                rng = parse_locator(ev.get("locator"))
                if ev.get("locator") and not rng:
                    raise S.ValidationError(f"locator {ev['locator']!r} must look like L10-L30")
                if rng:
                    norm["excerpt_hash"] = excerpt_hash(read_lines(self.root, ref, *rng, max_lines=None) or "")
                norm["ref"] = ref
            out.append(norm)
        return out

    @staticmethod
    def _cats(evs: list[dict]) -> set[str]:
        return {e.get("category") or S.EVIDENCE_CATEGORY.get(e["kind"], "other") for e in evs}

    def _passing_checks(self, evs: list[dict]) -> bool:
        """Есть свежая проверка, которая прошла (любого вида)."""
        return checks.best_level(self, evs) in ("verified", "check_passed")

    def support_level(self, evs: list[dict]) -> str:
        """Наибольшая подкреплённость, которую дают эти доказательства."""
        cats = self._cats(evs)
        level = checks.best_level(self, evs)
        if level in ("verified", "check_passed"):
            return level
        if "verifiable" in cats:
            return "sourced"
        if cats & {"attested", "external"}:
            return "attested"
        return "probable"

    def check_certainty(self, certainty: str | None, conf: float | None, evs: list[dict]) -> None:
        """Объявленный уровень не выше того, что дают доказательства."""
        if certainty is not None:
            if certainty not in S.DECLARABLE_CERTAINTIES:
                raise S.ValidationError(
                    f"certainty {certainty!r} cannot be declared; use one of {S.DECLARABLE_CERTAINTIES}. "
                    "verified comes only from a check that agent_memory ran (kind: check)")
            level = self.support_level(evs)
            order = ["sourced", "attested", "probable", "hypothesis", "unknown"]
            if level in order and certainty in ("sourced", "attested") and \
                    order.index(certainty) < order.index(level):
                raise S.ValidationError(f"certainty {certainty} is stronger than the evidence ({level})")
        if conf is not None:
            if not 0.0 <= float(conf) <= 1.0:
                raise S.ValidationError("confidence must be in [0, 1]")
            if float(conf) > 0.9 and checks.best_level(self, evs) != "verified":
                raise S.ValidationError("confidence > 0.9 requires a fresh passing pytest check (verified)")

    def _validate_new(self, rec: dict, evs: list[dict]) -> dict:
        rtype = rec.get("type")
        if rtype not in S.RECORD_TYPES:
            raise S.ValidationError(f"type {rtype!r} unknown; use one of {S.RECORD_TYPES}")
        statement = (rec.get("statement") or "").strip()
        if not statement:
            raise S.ValidationError("statement is empty")
        if len(statement) > MAX_STATEMENT:
            raise S.ValidationError(
                f"statement is {len(statement)} chars; a memory is one claim (<= {MAX_STATEMENT}). "
                "Split it, and keep raw detail in the episode archive or the source file."
            )
        section = rec.get("section")
        if rtype == "EPISODE" and section is None:
            section = "workflow"  # итог эпизода — запись о ходе работы
        if section not in S.SECTIONS:
            raise S.ValidationError(f"section {section!r}: every new record needs one of {S.SECTIONS} "
                                    "(code — structure; logic — rules and decisions; data — schemas and sources; "
                                    "workflow — how to run, test, deliver)")
        refuse_secrets(statement, rec.get("reason"), rec.get("subject"),
                       *[e.get("note") for e in evs if isinstance(e, dict)])
        if rtype in S.NEEDS_EVIDENCE and not evs:
            raise S.ValidationError(f"{rtype} needs evidence (file/code/commit/check/user/tool/url)")
        if rtype in S.NEEDS_NON_MODEL_EVIDENCE and evs and all(e["kind"] == "model" for e in evs):
            raise S.ValidationError(f"{rtype} cannot rest on model inference alone; record it as HYPOTHESIS")
        failing = [e for e in evs if e["kind"] == "check" and not checks.state(self, e)["satisfied"]]
        if failing:
            st = checks.state(self, failing[0])
            note = f" ({st['note']})" if st.get("note") else ""
            raise S.ValidationError(f"check did not confirm the claim: {st['method']} -> {st['result']}{note} "
                                    f"(expected {failing[0].get('check_expect')}); record a HYPOTHESIS or fix it")
        declared = rec.get("certainty")
        if rtype == "HYPOTHESIS" and declared not in (None, "hypothesis", "probable", "unknown"):
            raise S.ValidationError("a HYPOTHESIS stays hypothesis/probable/unknown; promote it with "
                                    "CONFIRM promote_to and a passing check")
        conf = rec.get("confidence")
        conf = float(conf) if conf is not None else None
        self.check_certainty(declared, conf, evs)
        if rtype == "HYPOTHESIS":
            certainty = declared or "hypothesis"
            if certainty not in ("hypothesis", "probable", "unknown"):
                raise S.ValidationError("a HYPOTHESIS stays hypothesis/probable/unknown; promote it with "
                                        "CONFIRM promote_to and a passing check")
        else:
            certainty = declared or self.support_level(evs)
        return {
            "type": rtype,
            "section": section,
            "statement": statement,
            "reason": rec.get("reason"),
            "subject": rec.get("subject"),
            "certainty": certainty,
            "confidence": conf,
            "tags": list(rec.get("tags") or []),
        }

    def current_records(self, rows: list[dict]) -> list[dict]:
        tr = self.store.transitions([r["id"] for r in rows])
        return [r for r in rows if self.is_current(r, tr.get(r["id"], []))]

    def find_duplicate(self, rtype: str, statement: str) -> str | None:
        key = normalize_statement(statement)
        rows = [r for r in self.store.records_of_type(rtype) if normalize_statement(r["statement"]) == key]
        current = self.current_records(rows)
        return current[0]["id"] if current else None

    def near_duplicates(self, rtype: str, statement: str, exclude: str | None = None) -> list[tuple[str, float]]:
        vec = self.embedder.embed(statement)
        hits = []
        for rid, _ in self.store.all_vectors():
            if rid == exclude:
                continue
            rec = self.store.get_record(rid)
            if not rec or rec["type"] != rtype or not self.is_current(rec):
                continue
            sim = cosine(vec, self.store.get_vector(rid))
            if sim >= NEAR_DUP:
                hits.append((rid, round(sim, 3)))
        return sorted(hits, key=lambda x: -x[1])[:3]

    # ================================================================ граф

    def ensure_episode_node(self, eid: str, task: str) -> None:
        self.store.upsert_knowledge_node({"id": eid, "kind": "episode", "label": task[:120],
                                          "terms": " ".join(split_identifier(task))})

    def resolve_target(self, ref: str, allow_create: bool = True) -> str:
        """Имя из диффа -> id узла. Неоднозначность и неизвестность — ошибка, не догадка."""
        ref = ref.strip()
        if _RECORD_ID.match(ref):
            if not self.store.get_record(ref):
                raise S.NotFound(f"record {ref} not found")
            return ref
        if ref.startswith("episode_"):
            if not self.store.get_node(ref):
                raise S.NotFound(f"episode {ref} not found")
            return ref
        if ref.startswith(("ext:", "concept:")):
            if not self.store.get_node(ref):
                if not allow_create:
                    raise S.NotFound(ref)
                kind = "external" if ref.startswith("ext:") else "concept"
                label = ref.split(":", 1)[1]
                self.store.upsert_knowledge_node({"id": ref, "kind": kind, "label": label,
                                                  "terms": " ".join(split_identifier(label))})
            return ref
        if ref.startswith(("code:", "file:")):
            node = self.store.get_node(ref)
            if node:
                cur = self.current_code_node(ref) if ref.startswith("code:") else node
                return cur["id"] if cur else ref
            if ref.startswith("file:"):
                return self._file_node(ref[5:], allow_create)
            path = ref[5:].split("::", 1)[0]
            p = inside_root(self.root, path)
            if p is not None and p.is_file():
                self.code.index([path])
                if self.store.get_node(ref):
                    return ref
            raise S.NotFound(f"code entity {ref} not found in this worktree; run `index`")
        if "/" in ref or re.search(r"\.(py|md|ts|js|sql|ya?ml|json|sh|toml|ini|txt)$", ref):
            return self._file_node(ref, allow_create)
        cands = [n for n in self.store.find_nodes_by_label(ref) if n["kind"] not in ("file", "external", "doc")]
        exact = [n for n in cands if n["label"] == ref]
        pick = exact or cands
        if len(pick) == 1:
            return pick[0]["id"]
        if not pick:
            raise S.NotFound(f"symbol {ref!r} not in this worktree's code index; run `index`, "
                             "or use ext:/concept: for non-code")
        raise S.ValidationError(f"symbol {ref!r} is ambiguous: {[n['id'] for n in pick[:8]]}")

    def _file_node(self, rel: str, allow_create: bool) -> str:
        nid = f"file:{rel}"
        if self.store.get_node(nid):
            return nid
        p = inside_root(self.root, rel)
        if p is None or not p.is_file():
            raise S.NotFound(f"file {rel} not found under repo root")
        if rel.endswith(".py"):
            self.code.index([rel])
            if self.store.get_node(nid):
                return nid
        if not allow_create:
            raise S.NotFound(nid)
        kind = "doc" if rel.endswith((".md", ".txt", ".rst")) else "file"
        with self.store.code_transaction():
            self.store.c.upsert_node({"id": nid, "kind": kind, "label": rel, "path": rel,
                                      "terms": " ".join(split_identifier(rel))})
        return nid

    # ======================================================== примитивы записи

    def refuse_ignored(self, paths, what: str) -> None:
        """Источник в игнорируемом git файле не принимается: он никогда не попадёт в историю,
        а хэш его содержимого (например, .env) хранить нельзя."""
        bad = self.view.ignored([p for p in paths if p])
        if bad:
            raise S.ValidationError(
                f"{what} {', '.join(bad)} is ignored by git (.gitignore): memory does not cite or hash ignored "
                "files (they may hold secrets and never reach history). Cite a tracked file instead.")

    def _node_path(self, nid: str) -> str | None:
        if nid.startswith("file:"):
            return nid[5:]
        if nid.startswith("code:"):
            node = self.store.get_node(nid)
            return node["path"] if node else nid[5:].split("::", 1)[0]
        return None

    def record_paths(self, evs: list[dict], targets=()) -> list[str]:
        """Файлы, о которых запись: её доказательства, проверки и связи с кодом."""
        paths: set[str] = set()
        for e in evs:
            ref = e.get("ref") or ""
            if e["kind"] in ("file", "doc", "test"):
                paths.add(ref)
            elif e["kind"] in ("code", "check"):
                p = self._node_path(ref) if ref.startswith(("code:", "file:")) else ref.split("::", 1)[0]
                if p:
                    paths.add(p)
        for t in targets:
            p = self._node_path(t)
            if p:
                paths.add(p)
        return sorted(p for p in paths if p and inside_root(self.root, p) is not None)

    def paths_of_record(self, rid: str) -> list[str]:
        targets = [e["dst"] if e["src"] == rid else e["src"] for e in self.store.k.edges_touching([rid])]
        return self.record_paths(self.store.evidence_for(rid), targets)

    def _insert(self, fields: dict, evs: list[dict], episode: str | None, paths: list[str] | None = None) -> str:
        rid = self.store.next_id(S.ID_PREFIX[fields["type"]])
        ts = now()
        obs = self.view.observed(paths if paths is not None else self.record_paths(evs))
        rec = {**fields, "id": rid, "status": "active", "created_from": episode, "valid_from": episode,
               "created_at": ts, "updated_at": ts, "observed_commit": obs["commit"],
               "observed_branch": obs["branch"], "observed_scope": obs["scope"], "observed_dirty": obs["dirty"],
               "observed_patch": obs["patch"]}
        vec = to_blob(self.embedder.embed(f"{fields['statement']} {fields.get('reason') or ''} "
                                          f"{fields.get('subject') or ''}"))
        self.store.insert_record(rec, vec, episode)
        for ev in evs:
            self.store.add_evidence(rid, ev, episode)
        if episode and self.store.get_node(episode):
            self.store.add_edge(rid, "DISCOVERED_IN", episode, "asserted", episode=episode)
        return rid

    def add(self, record: dict, episode: str | None = None) -> dict:
        """Одна запись. Дубль превращается в CONFIRM, спор по subject — ConflictError."""
        return self.commit({"ops": [{"op": "ADD", "record": record}]}, episode=episode)

    def update(self, rid: str, patch: dict, expected_version: int | None, reason: str | None = None,
               episode: str | None = None, evidence: list | None = None) -> dict:
        op = {"op": "UPDATE", "id": rid, "patch": patch, "reason": reason, "expected_version": expected_version,
              "evidence": evidence or []}
        return self.commit({"ops": [op]}, episode=episode)

    def invalidate(self, rid: str, reason: str, expected_version: int | None, evidence: list | None = None,
                   episode: str | None = None) -> dict:
        op = {"op": "INVALIDATE", "id": rid, "reason": reason, "evidence": evidence or [],
              "expected_version": expected_version}
        return self.commit({"ops": [op]}, episode=episode)

    def link(self, a: str, rel: str, b: str, origin: str = "asserted", episode: str | None = None) -> dict:
        op = {"op": "LINK", "from": a, "rel": rel, "to": b, "origin": origin}
        return self.commit({"ops": [op]}, episode=episode)

    def commit(self, diff: dict, episode: str | None = None) -> dict:
        from .diff import apply_diff

        return apply_diff(self, diff, episode=episode)

    def index(self, paths: list[str] | None = None, progress: bool = False, force: bool = False) -> dict:
        rep = self.code.index(paths, progress=progress, force=force)
        self.view.refresh()
        with self.store.code_transaction():
            self.store.c.set_meta("indexed_head", self.view.head or "")
        return rep

    # ========================================================== производное

    def repair(self) -> dict:
        """Восстановить производные файлы из базы: JSON-архивы эпизодов. Идемпотентно."""
        from .context import export_archive

        done, moved = [], []
        for ep in self.store.episodes_needing_export():
            res = export_archive(self, ep)
            done.append(ep["id"])
            if res.get("moved_aside"):
                moved.append(res["moved_aside"])
        return {"archives_exported": done, "archives_moved_aside": moved}
