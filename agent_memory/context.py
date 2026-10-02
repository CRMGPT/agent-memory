"""Контекст и эпизоды: build / begin / expand / note / end / drop / compact.

Набор памяти собирается из рабочего состояния, рамки задачи, отобранных записей и
текущего кода этой рабочей копии — под бюджет, у каждого блока написано, зачем он здесь.

Эпизод принадлежит сессии, которая держит аренду рабочей копии (session.py): чужой
эпизод сессия не дописывает и не закрывает.

Завершение эпизода (end) — одна транзакция SQLite: дифф знаний, закрытие эпизода,
полный архив (задача, заметки, дифф, результат) и новое рабочее состояние. Файлы на
диске — JSON-архив и working.md — производные копии из базы: их пишет следующий шаг,
а при сбое восстанавливает `repair` (любая команда вызывает его сама). Поэтому:
  * отказ до COMMIT — не сохранено ничего, эпизод открыт, заметки на месте;
  * отказ после COMMIT — база сохранена, нужен только производный файл;
  * повтор end с тем же диффом — не задваивает записи.

Числа размера — приблизительная оценка (символы/4), а не токены модели. Память не видит
и не очищает переписку агента: она лишь не держит в себе временные заметки после end.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import checks, faults
from . import schema as S
from .codegraph import read_lines
from .diff import diff_identity
from .memory import refuse_secrets
from .retrieval import Retriever
from .session import Leases
from .store import now
from .taskframe import build_frame
from .textindex import estimate_tokens

DEFAULT_BUDGET = 6000
WORKING_LIMIT = 6000
MAX_SNIPPET_LINES = 60
_REC_REF = re.compile(r"\b(?:" + "|".join(S.ID_PREFIX.values()) + r")_\d{5}\b")

WORKING_TEMPLATE = "\n\n".join(f"# {s}\n" for s in S.WORKING_SECTIONS)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def export_archive(mem, ep: dict) -> dict:
    """JSON-архив эпизода из канонической записи в базе. Идемпотентно.

    Файл с тем же содержимым — уже готов. С другим (остался от сбоя старой версии) —
    откладывается рядом как `.orphan-<время>.json`, ничего не удаляется.
    """
    path = mem.dir / "episodes" / f"{ep['id'].split('_')[-1]}.json"
    text = ep["archive_json"]
    moved = None
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            moved = path.with_name(f"{path.stem}.orphan-{now().replace(':', '')}.json")
            os.replace(path, moved)
    if not path.exists():
        faults.hit("export_archive")
        _atomic_write(path, text, 0o444)
    with mem.store.transaction():
        mem.store.update_episode(ep["id"], archive_exported_at=now(),
                                 archive_path=str(path.relative_to(mem.dir)))
    return {"path": str(path), "moved_aside": str(moved) if moved else None}


class Context:
    def __init__(self, mem, budget: int = DEFAULT_BUDGET, working_limit: int = WORKING_LIMIT,
                 session: str | None = None):
        self.mem = mem
        self.store = mem.store
        self.scope = mem.scope
        self.budget = budget
        self.working_limit = working_limit
        self.session = session or os.environ.get("AGENT_MEMORY_SESSION") or None
        self.leases = Leases(self.store, self.scope)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.scope)
        self.working_path: Path = mem.dir / "working" / f"{safe}.md"

    # ============================================================ аренда

    def start_session(self, label: str | None = None) -> str:
        self.session = self.leases.start(label)
        return self.session

    def _authorize(self) -> None:
        """Пишущая операция: своя аренда, продление, привязка закоммиченной работы."""
        self.leases.require(self.session)
        self.mem.anchor_pending()

    def _own_episode(self, required: bool = True) -> dict | None:
        ep = self.store.open_episode(self.scope)
        if ep is None:
            if required:
                raise S.ValidationError("no open episode in this worktree; `begin` one first")
            return None
        if ep.get("owner_token") != self.session:
            owner = ep.get("owner_token") or "a session from before schema v2"
            raise S.LeaseError(f"episode {ep['id']} belongs to {owner}, not to your session {self.session}. "
                               "Sessions do not write into or close each other's episodes; if that session "
                               "is gone, `session adopt` takes the episode over with a recorded note.")
        return ep

    def adopt(self) -> dict:
        """Забрать открытый эпизод мёртвой или прежней сессии. Аренда этой копии — у нас."""
        self._authorize()
        ep = self.store.open_episode(self.scope)
        if not ep:
            raise S.ValidationError("no open episode to adopt")
        if ep.get("owner_token") == self.session:
            return {"episode": ep["id"], "adopted": False}
        with self.store.transaction():
            self.store.update_episode(ep["id"], owner_token=self.session)
            self.store.add_note(ep["id"], "recovery", f"session {self.session} adopted the episode from "
                                f"{ep.get('owner_token') or 'pre-v2 store'}", None)
        return {"episode": ep["id"], "adopted": True, "from": ep.get("owner_token")}

    # ============================================================ working.md

    def read_working(self) -> str:
        row = self.store.get_working(self.scope)
        if row:
            return row["text"]
        if self.working_path.exists():  # хранилище до v2: файл был главным
            return self.working_path.read_text(encoding="utf-8")
        return WORKING_TEMPLATE

    def _check_working(self, text: str) -> None:
        if len(text) > self.working_limit:
            raise S.ValidationError(
                f"working.md is {len(text)} chars, limit {self.working_limit}. It holds only current state: "
                "move settled knowledge into memory with a diff and keep a record id here instead."
            )

    def sync_working_file(self) -> bool:
        """Производная копия working.md на диске. True — файл записан или уже совпадал."""
        row = self.store.get_working(self.scope)
        if not row:
            return True
        if row.get("file_synced_hash") == _digest(row["text"]) and self.working_path.exists():
            return True
        faults.hit("write_working")
        _atomic_write(self.working_path, row["text"])
        with self.store.transaction():
            self.store.mark_working_synced(self.scope, _digest(row["text"]))
        return True

    def _sync_or_report(self, what: str) -> None:
        try:
            self.sync_working_file()
        except Exception as exc:  # noqa: BLE001 — сбой файловой системы после сохранения в базу
            raise S.DerivedSyncError(f"database SAVED ({what}); working.md file not written "
                                     f"({type(exc).__name__}: {exc}); run `repair`") from exc

    def write_working(self, text: str) -> dict:
        refuse_secrets(text, what="the working state")
        self._authorize()
        self._check_working(text)
        with self.store.transaction():
            self.store.put_working(self.scope, text)
        self._sync_or_report("working state")
        missing = [s for s in S.WORKING_SECTIONS if f"# {s}" not in text]
        return {"chars": len(text), "missing_sections": missing}

    def _compacted(self, text: str) -> tuple[str, list[dict]]:
        """Убрать строки списка со ссылками на записи, которые здесь больше не действуют. Пересказа нет."""
        kept, removed = [], []
        for line in text.splitlines():
            ids = _REC_REF.findall(line)
            dead = [i for i in ids if (r := self.store.get_record(i)) and self.mem.status(r) != "active"]
            if dead and line.lstrip().startswith(("-", "*")):
                removed.append({"line": line.strip(), "inactive": dead})
            else:
                kept.append(line)
        return "\n".join(kept) + ("\n" if text.endswith("\n") else ""), removed

    def compact(self) -> dict:
        self._authorize()
        text = self.read_working()
        new, removed = self._compacted(text)
        if new != text:
            with self.store.transaction():
                self.store.put_working(self.scope, new)
            self._sync_or_report("compacted working state")
        return {"chars_before": len(text), "chars_after": len(new), "removed_lines": removed,
                "over_limit": len(new) > self.working_limit, "limit": self.working_limit}

    # ================================================================ сборка

    def _record_block(self, hit, with_sources: bool, shown: set[str] | None = None) -> str:
        rec = self.store.get_record(hit.id)
        d = self.mem.describe(rec)
        lines = [f"### [{rec['id']}] v{rec['version']} {rec['type']} · {rec.get('section') or 'unclassified'} · "
                 f"{d['certainty_here']} · {d['status_here']}"
                 + (f" · conf {rec['confidence']:.2f}" if rec.get("confidence") is not None else ""),
                 rec["statement"]]
        if rec.get("reason"):
            lines.append(f"Reason: {rec['reason']}")
        if d["scope_note"]:
            lines.append(f"⚠ SCOPE: {d['scope_note']}")
        if d["status_here"] != "active":
            lines.append(f"Status here: {d['status_here']}"
                         + (f"; superseded by {rec['superseded_by']}" if rec.get("superseded_by") else ""))
        if rec.get("stale_reason"):
            lines.append(f"⚠ STALE: {rec['stale_reason']} — re-verify before relying on it")
        lines.append("Why included: " + "; ".join(hit.reasons[:3]))
        srcs = self.mem._sources(rec["id"], max_lines=MAX_SNIPPET_LINES if with_sources else 0)
        if srcs:
            parts = []
            for s in srcs[:4]:
                desc = f"{s['kind']} {s.get('current_ref') or s['ref']}" + (f" {s['locator']}" if s.get("locator") else "")
                if s.get("label"):
                    desc += f" ({s['label']})"
                elif s["verification"] != "not_applicable":
                    desc += f" ({s['verification']})"
                parts.append(desc)
            lines.append("Evidence: " + "; ".join(parts))
            if with_sources:
                for s in srcs[:2]:
                    if s.get("excerpt"):
                        lines.append(f"```\n{s['excerpt']}\n```")
                        if shown is not None and s["kind"] == "code":
                            shown.add(s.get("current_ref") or s["ref"])
        return "\n".join(lines)

    def _code_block(self, hit) -> str | None:
        node = self.store.get_node(hit.id)
        if not node or node["kind"] not in ("function", "method", "class", "test"):
            return None
        if node["status"] != "present":
            return f"### {node['id']} — {node['status'].upper()}\nWhy included: {'; '.join(hit.reasons[:2])}"
        text = read_lines(self.mem.root, node["path"], node["start_line"], node["end_line"],
                          max_lines=MAX_SNIPPET_LINES)
        if text is None:
            return None
        return (f"### {node['label']} — {node['path']} L{node['start_line']}-L{node['end_line']}\n"
                f"Why included: {'; '.join(hit.reasons[:2])}\n```python\n{text}\n```")

    def _assemble(self, sel, budget: int, title: str, include_working: bool, freshness: dict) -> dict:
        parts: list[str] = []
        used = 0
        working = self.read_working() if include_working else ""
        if include_working and not [ln for ln in working.splitlines() if ln.strip() and not ln.startswith("# ")]:
            working = "(empty: fill it with `working set <file>` once the goal is clear)"
        header = f"# Memory context: {title}"
        frame = sel.frame
        frame_txt = "\n".join(f"- {k}: {', '.join(map(str, frame.get(k) or [])) or '—'}"
                              for k in ("entities", "files", "systems", "concepts", "constraints"))
        stale_banner = "" if freshness["fresh"] else (
            f"⚠ CODE INDEX OUT OF DATE for this worktree: {freshness['total']} file(s) changed since `index` "
            f"(e.g. {', '.join((freshness['changed'] + freshness['new'] + freshness['deleted'])[:3])}). "
            "Run `index`; line numbers and code links below may be off.")
        for block in (header, stale_banner, f"## Working memory\n{working.strip()}" if include_working else "",
                      f"## Task frame\n{frame_txt}"):
            if block:
                parts.append(block)
                used += estimate_tokens(block)

        injected, code_injected, skipped = [], [], 0
        rec_budget = used + int((budget - used) * 0.6)
        mem_parts = []
        shown: set[str] = set()  # код, уже показанный как доказательство записи, второй раз не печатаем
        for i, hit in enumerate(sel.records):
            trial: set[str] = set()
            block = self._record_block(hit, with_sources=i < 3, shown=trial)
            cost = estimate_tokens(block)
            if used + cost > rec_budget and not (hit.pinned and used + cost <= budget):
                block = self._record_block(hit, with_sources=False)
                cost = estimate_tokens(block)
                if used + cost > rec_budget and not (hit.pinned and used + cost <= budget):
                    skipped += 1
                    continue
            mem_parts.append(block)
            injected.append(hit.id)
            shown |= trial if "```" in block else set()
            used += cost
        if mem_parts:
            parts.append("## Relevant memory\n" + "\n\n".join(mem_parts))
        # Текст кода грузим только для того, что названо в задаче или привязано к выданной памяти.
        linked: set[str] = set()
        for rid in injected:
            for e in self.store.edges_of(rid):
                if e["origin"] != "deterministic":
                    linked.add(e["dst"] if e["src"] == rid else e["src"])
        code_parts, nearby = [], []
        for hit in sel.code:
            node = self.store.get_node(hit.id) or {}
            if node.get("kind") not in ("function", "method", "class", "test") or hit.id in shown:
                continue
            if hit.hop == 0 or hit.id in linked:
                block = self._code_block(hit)
                cost = estimate_tokens(block) if block else 0
                if block and used + cost <= budget and len(code_parts) < 8:
                    code_parts.append(block)
                    code_injected.append(hit.id)
                    used += cost
                    continue
                skipped += bool(block)
            if len(nearby) < 12:
                nearby.append(f"- {node['label']} — {node['path']}:{node['start_line']} ({hit.reasons[0][:70]})")
        if code_parts:
            parts.append("## Current code\n" + "\n\n".join(code_parts))
        if nearby:
            block = "## Nearby code (not loaded)\n" + "\n".join(nearby)
            if used + estimate_tokens(block) <= budget:
                parts.append(block)
                used += estimate_tokens(block)
        total = self.store.count_records()
        parts.insert(1, f"_~{used} of {budget} estimated tokens (chars/4) · {len(injected)} memories of "
                        f"{len(sel.records)} candidates, {total} records in store · "
                        f"{len(code_injected)} code blocks · {skipped} skipped for budget · worktree {self.scope}"
                        f"{' @' + (self.mem.view.head or '')[:10] if self.mem.view.head else ''}. "
                        "Missing something? `expand \"<what>\"` (lazy lookup)._")
        md = "\n\n".join(p for p in parts if p)
        return {"markdown": md, "pack_tokens_est": estimate_tokens(md), "injected": injected,
                "code_injected": code_injected, "skipped": skipped}

    def _source_check_rate(self, ids: list[str]) -> float | None:
        """Доля источников выданных записей, которые сейчас найдены и совпадают с записанным."""
        checked = ok = 0
        for rid in ids:
            for s in self.mem._sources(rid, max_lines=0):
                if s["verification"] in ("unchanged", "changed", "missing", "present", "needs_reindex",
                                         "check_ok", "check_stale", "check_failed"):
                    checked += 1
                    ok += s["verification"] in ("unchanged", "present", "check_ok")
        return round(ok / checked, 3) if checked else None

    def build(self, task: str, frame_extra: dict | None = None, budget: int | None = None,
              exclude: set[str] | None = None, include_working: bool = True, event: str = "build",
              episode: str | None = None, sections: tuple[str, ...] | None = None) -> dict:
        self.mem.view.refresh()  # набор — по текущему состоянию кода этой копии
        frame = build_frame(task, frame_extra)
        freshness = self.mem.code_freshness()
        sel = Retriever(self.mem, sections=sections).retrieve(frame, exclude=exclude)
        pack = self._assemble(sel, budget or self.budget, task, include_working, freshness)
        stats = {**sel.stats, "pack_tokens_est": pack["pack_tokens_est"], "nodes_injected": len(pack["injected"]),
                 "code_injected": len(pack["code_injected"]), "memory_size": self.store.count_records(),
                 "source_match_rate": self._source_check_rate(pack["injected"]),
                 "code_index_fresh": freshness["fresh"]}
        with self.store.transaction():
            self.store.log_metric(event, stats, episode)
        return {**pack, "frame": frame, "stats": stats, "code_freshness": freshness}

    # ============================================================== эпизоды

    def current(self) -> dict | None:
        return self.store.open_episode(self.scope)

    def begin(self, task: str, frame_extra: dict | None = None, budget: int | None = None,
              sections: tuple[str, ...] | None = None) -> dict:
        refuse_secrets(task, frame_extra, what="the task text")
        started = None
        if not self.session and not self.leases.status():
            started = self.start_session()  # первая команда в свободной копии сама берёт аренду
        self._authorize()
        ep = self.current()
        if ep:
            if ep.get("owner_token") == self.session:
                raise S.ValidationError(f"episode {ep['id']} is still open; `end` or `drop` it first")
            self._own_episode()  # чужой эпизод: понятный отказ
        index_report = self.mem.index()  # своя копия, своя аренда: индекс обновляем перед поиском
        eid = self.store.next_id("episode")
        frame = build_frame(task, frame_extra)
        with self.store.transaction():
            self.store.insert_episode(eid, task, frame, self.scope, self.session)
            self.mem.ensure_episode_node(eid, task)
        pack = self.build(task, frame_extra, budget, episode=eid, sections=sections)
        with self.store.transaction():
            self.store.update_episode(eid, injected=pack["injected"] + pack["code_injected"])
        return {"episode": eid, "session": self.session, "session_started": bool(started),
                "index": {k: (len(v) if isinstance(v, list) else v) for k, v in index_report.items()}, **pack}

    def expand(self, query: str, budget: int = 2000, sections: tuple[str, ...] | None = None) -> dict:
        refuse_secrets(query, what="the query")  # запрос сохраняется заметкой эпизода
        """Ленивая подгрузка посреди работы: «page fault» памяти, без повтора уже выданного."""
        self._authorize()
        ep = self._own_episode(required=False)
        if not self.mem.code_freshness()["fresh"]:
            self.mem.index()
        exclude = set(ep["injected"]) if ep else set()
        pack = self.build(query, budget=budget, exclude=exclude, include_working=False, event="expand",
                          episode=ep["id"] if ep else None, sections=sections)
        if ep:
            with self.store.transaction():
                self.store.update_episode(ep["id"],
                                          injected=ep["injected"] + pack["injected"] + pack["code_injected"])
                self.store.add_note(ep["id"], "expand", query, None)
        return pack

    def note(self, text: str, kind: str = "note", ref: str | None = None) -> dict:
        refuse_secrets(text, kind, ref, what="the note")
        self._authorize()
        ep = self._own_episode()
        with self.store.transaction():
            seq = self.store.add_note(ep["id"], kind, text, ref)
        return {"episode": ep["id"], "seq": seq, "ref": f"note:{ep['id']}#{seq}"}

    def _scratch_tokens_est(self, ep: dict) -> dict:
        notes = sum(estimate_tokens(n["text"]) for n in self.store.notes(ep["id"]))
        packs = sum(m["data"].get("pack_tokens_est", 0) for m in self.store.metrics()
                    if m["episode_id"] == ep["id"] and m["event"] in ("build", "expand"))
        return {"notes_tokens_est": notes, "packs_returned_tokens_est": packs,
                "working_tokens_est": estimate_tokens(self.read_working())}

    def _archive_payload(self, ep: dict, status: str, extra: dict) -> str:
        record = {"id": ep["id"], "scope": ep.get("scope"), "owner_token": ep.get("owner_token"),
                  "task": ep["task"], "status": status, "frame": ep["frame"], "started_at": ep["started_at"],
                  "ended_at": now(), "injected": ep["injected"], "notes": self.store.notes(ep["id"]),
                  "code_state": self.mem.view.observed(), **extra}
        return json.dumps(record, ensure_ascii=False, indent=1)

    def _finish_derived(self, ep_id: str) -> dict:
        """Производные файлы после COMMIT. Сбой здесь не теряет данные: их восстановит repair."""
        pending = []
        ep = self.store.get_episode(ep_id)
        try:
            export_archive(self.mem, ep)
        except Exception as exc:  # noqa: BLE001 — любой сбой файловой системы
            pending.append(f"episode archive ({type(exc).__name__}: {exc})")
        try:
            self.sync_working_file()
        except Exception as exc:  # noqa: BLE001
            pending.append(f"working.md ({type(exc).__name__}: {exc})")
        return {"pending_derived": pending}

    def end(self, diff: dict | None, summary: str | None = None, working: str | None = None) -> dict:
        refuse_secrets(diff, summary, working, what="the episode result")
        self._authorize()
        ops = list((diff or {}).get("ops") or [])
        if summary:
            ops.append({"op": "ADD", "record": {"type": "EPISODE", "statement": summary,
                                                "evidence": [{"kind": "episode", "ref": "@episode"}]}})
        ep = self.store.open_episode(self.scope)
        if ep is None:
            return self._replay_end(ops, diff)
        self._own_episode()
        if working is not None:
            self._check_working(working)
        for op in ops:  # итог эпизода ссылается на сам эпизод
            for ev in (op.get("record") or {}).get("evidence") or []:
                if ev.get("ref") == "@episode":
                    ev["ref"] = ep["id"]
        full = {"ops": ops, **({"diff_id": diff["diff_id"]} if diff and diff.get("diff_id") else {})}
        diff_id, _ = diff_identity(full, ep["id"]) if ops else (None, None)
        # Карта кода ЭТОЙ рабочей копии — к текущим файлам до проверки ссылок.
        code_map = self._refresh_code_map(ep, ops)
        snapshot = self.mem.code.disk_state()
        # Проверки (pytest) — до транзакции: долгий запуск не держит блокировку общей базы.
        full["ops"] = checks.prepare_ops(self.mem, ops)
        scratch = self._scratch_tokens_est(ep)
        working_before = self.read_working()

        # Одна транзакция: знания, закрытие эпизода, полный архив, новое рабочее состояние.
        try:
            report, removed, metrics = self._end_transaction(ep, full, ops, diff, summary, working, diff_id,
                                                             scratch, working_before, snapshot)
        except S.MemoryError_:
            raise
        except Exception as exc:  # сбой базы или диска до COMMIT: транзакция откатилась целиком
            raise S.MemoryError_(f"nothing saved for {ep['id']} ({type(exc).__name__}: {exc}); the episode is "
                                 "still open with its notes; retry `end`") from exc
        faults.hit("end_after_commit")
        derived = self._finish_derived(ep["id"])
        result = {"episode": ep["id"], "saved": True, "commit": report, "diff_id": diff_id,
                  "code_map": code_map, "compaction": {"removed_lines": removed}, **metrics, **derived}
        if derived["pending_derived"]:
            raise S.DerivedSyncError(
                f"database SAVED for {ep['id']} (knowledge, episode close, archive content); derived file(s) "
                f"not written: {', '.join(derived['pending_derived'])}. Nothing is lost: run "
                "`python3 -m agent_memory repair` (any next command also repairs).")
        return result

    def _paths_in_ops(self, ops: list[dict]) -> set[str]:
        paths: set[str] = set()
        for op in ops:
            evs = list(op.get("evidence") or []) + list((op.get("record") or {}).get("evidence") or [])
            evs = [e for e in evs if isinstance(e, dict) and e.get("kind") and isinstance(e.get("ref"), str)]
            targets = [t for t in (op.get("from"), op.get("to")) if isinstance(t, str)]
            try:
                paths.update(self.mem.record_paths(evs, targets))
            except Exception:  # noqa: BLE001 — разбор ссылок ниже даст понятную ошибку
                continue
        return paths

    def _refresh_code_map(self, ep: dict, ops: list[dict]) -> dict:
        """Привести карту кода своей копии к файлам на диске (изменённые, новые неотслеживаемые,
        удалённые, переименованные — по правилам index). Без изменений — ничего не делает.
        Ошибка — эпизод открыт, ничего не сохранено, повтор безопасен."""
        if self.mem.code_freshness()["fresh"]:
            return {"reindexed": False}
        try:
            faults.hit("end_reindex")
            rep = self.mem.index()
        except S.MemoryError_:
            raise
        except Exception as exc:
            raise S.MemoryError_(f"nothing saved for {ep['id']}: the code map of this worktree could not be "
                                 f"updated ({type(exc).__name__}: {exc}); the episode is still open with its "
                                 "notes; retry `end`") from exc
        broken = sorted(set(rep["parse_errors"]) & self._paths_in_ops(ops))
        if broken:
            raise S.ValidationError(f"nothing saved for {ep['id']}: {', '.join(broken)} does not parse, so the "
                                    "knowledge about it cannot be checked against current code; fix the file and "
                                    "retry `end`")
        return {"reindexed": True, "files_parsed": rep["files_parsed"], "moved": len(rep["moved"]),
                "missing": len(rep["missing"]), "stale_records": len(rep["stale_records"]),
                "parse_errors": rep["parse_errors"]}

    def _recheck_owner(self, ep_id: str) -> None:
        """Под блокировкой записи: аренда и эпизод всё ещё наши (их могли забрать, пока шли проверки)."""
        lease = self.leases.status()
        ep = self.store.get_episode(ep_id)
        if not lease or lease["token"] != self.session or not ep or ep.get("owner_token") != self.session                 or ep["status"] != "open":
            raise S.LeaseError(f"while this command ran, ownership changed: worktree lease is "
                               f"{lease and lease['token']}, episode {ep_id} owner {ep and ep.get('owner_token')} "
                               f"status {ep and ep['status']}. Nothing saved by this command.")

    def _end_transaction(self, ep, full, ops, diff, summary, working, diff_id, scratch, working_before,
                         snapshot=None):
        with self.store.transaction(commit_fault="end_commit"):
            self._recheck_owner(ep["id"])
            moved = self.mem.code.changed_since(snapshot) if snapshot is not None else []
            if moved:
                raise S.ConflictError(f"code changed while `end` was running ({', '.join(moved[:5])}): nothing "
                                      f"saved for {ep['id']}, the episode is still open; finish the edit and retry "
                                      "`end`", moved)
            faults.hit("end_before_commit")
            report = self.mem.commit(full, episode=ep["id"]) if ops else {}
            new_working = working if working is not None else working_before
            new_working, removed = self._compacted(new_working)
            archive = self._archive_payload(ep, "committed", {
                "diff": diff, "summary": summary, "commit_report": report,
                "working_before": working_before, "working_after": new_working})
            self.store.put_working(self.scope, new_working)
            self.store.update_episode(ep["id"], status="committed", ended_at=now(), summary=summary,
                                      diff_id=diff_id, archive_json=archive)
            self.store.drop_notes(ep["id"])  # заметки уже в архиве этой же транзакции
            metrics = {**scratch, "working_tokens_est_after": estimate_tokens(new_working)}
            self.store.log_metric("end", metrics, ep["id"])
        return report, removed, metrics

    def _replay_end(self, ops: list[dict], diff: dict | None) -> dict:
        """Повтор end после сбоя связи или процесса: эпизод уже закрыт этим же диффом."""
        last = self.store.last_closed_episode(self.scope)
        if last and last.get("owner_token") == self.session and last["status"] == "committed":
            for op in ops:
                for ev in (op.get("record") or {}).get("evidence") or []:
                    if ev.get("ref") == "@episode":
                        ev["ref"] = last["id"]
            full = {"ops": ops, **({"diff_id": diff["diff_id"]} if diff and diff.get("diff_id") else {})}
            diff_id, _ = diff_identity(full, last["id"]) if ops else (None, None)
            if diff_id == last.get("diff_id"):
                derived = self._finish_derived(last["id"])
                if derived["pending_derived"]:
                    raise S.DerivedSyncError(f"database already SAVED for {last['id']}; derived file(s) still "
                                             f"not written: {', '.join(derived['pending_derived'])}; run `repair`")
                return {"episode": last["id"], "saved": True, "replayed": True,
                        "commit": json.loads(last["archive_json"]).get("commit_report"), **derived}
        raise S.ValidationError("no open episode in this worktree; nothing was saved by this call")

    def drop_episode(self, reason: str = "") -> dict:
        refuse_secrets(reason, what="the drop reason")
        self._authorize()
        ep = self._own_episode()
        with self.store.transaction(commit_fault="end_commit"):
            self._recheck_owner(ep["id"])
            archive = self._archive_payload(ep, "dropped", {"reason": reason})
            self.store.update_episode(ep["id"], status="dropped", ended_at=now(), archive_json=archive)
            dropped = self.store.drop_notes(ep["id"])
            self.store.log_metric("drop", {"notes_dropped": dropped}, ep["id"])
        derived = self._finish_derived(ep["id"])
        if derived["pending_derived"]:
            raise S.DerivedSyncError(f"database SAVED (episode {ep['id']} dropped and archived); derived file(s) "
                                     f"not written: {', '.join(derived['pending_derived'])}; run `repair`")
        return {"episode": ep["id"], "notes_dropped": dropped, **derived}
