"""Дифф знаний: единственный путь записи в смысловую память.

Дифф — список операций ADD / UPDATE / INVALIDATE / CONFIRM / SUPERSEDE / RESOLVE /
LINK / UNLINK. Применяется одной транзакцией: при любой ошибке не записывается ничего.

Три защиты от потери и задвоения при параллельной работе:

* `expected_version` — операция над существующей записью (UPDATE, SUPERSEDE, INVALIDATE,
  RESOLVE, CONFIRM с promote_to) указывает версию, которую агент прочитал. Запись
  успела измениться — ConflictError, а не «кто записал последним, тот прав».
* `diff_id` — повтор того же диффа (после таймаута, обрыва связи) возвращает прежний
  результат и ничего не пишет. Без явного id он считается от содержимого и эпизода.
  Тот же id с другим содержимым — ошибка.
* перемены статуса пишутся с коммитом и рабочей копией (view.py): закрытие записи
  в ветке не закрывает её на main, пока ветка не влита.

`@имя` внутри диффа указывает на запись, добавленную раньше в этом же диффе.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import checks
from . import schema as S
from .textindex import to_blob

OPS = ("ADD", "UPDATE", "INVALIDATE", "CONFIRM", "SUPERSEDE", "RESOLVE", "LINK", "UNLINK")
PATCHABLE = {"reason", "tags", "confidence", "certainty", "subject", "section"}
PROMOTABLE_FROM = {"HYPOTHESIS", "OBSERVATION", "QUESTION"}
PROMOTABLE_TO = {"FACT", "DECISION", "CONSTRAINT", "BUG", "TEST_RESULT"}
NO_INHERIT = {"DISCOVERED_IN", "SUPERSEDES"}
NEEDS_VERSION = {"UPDATE", "SUPERSEDE", "INVALIDATE", "RESOLVE"}


def _report() -> dict[str, Any]:
    return {k: [] for k in ("added", "confirmed", "duplicates_converted", "updated", "invalidated",
                            "superseded", "resolved", "linked", "unlinked", "warnings")}


def _canon(obj):
    """Дифф как его прислал агент: выполненная проверка заменяется исходным запросом."""
    if isinstance(obj, checks.Prepared):
        return _canon(getattr(obj, "request", dict(obj)))
    if isinstance(obj, dict):
        return {k: _canon(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canon(v) for v in obj]
    return obj


def content_hash(diff: dict) -> str:
    body = {k: _canon(v) for k, v in diff.items() if k != "diff_id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def diff_identity(diff: dict, episode: str | None) -> tuple[str, str]:
    chash = content_hash(diff)
    return (diff.get("diff_id") or f"auto:{episode or '-'}:{chash[:32]}"), chash


DIFF_FORMS = ('a list of records [{"type": ..., "section": ..., "statement": ..., "evidence": [...]}], '
              'one such record, or {"ops": [{"op": "ADD", "record": {...}}, ...]}')


def _need_evidence_list(value, where: str) -> None:
    """Доказательство — объект или строка `вид:ссылка` (её разбирает Memory._norm_evidence)."""
    if value is not None and (not isinstance(value, list) or not all(isinstance(v, (dict, str)) for v in value)):
        raise S.ValidationError(f"{where} must be a list of objects (or `kind:ref` strings)")


def _need_links(value, where: str) -> None:
    if value is not None and (not isinstance(value, list) or not all(
            isinstance(v, dict) and isinstance(v.get("to"), str) for v in value)):
        raise S.ValidationError(f'{where} must be a list of objects like {{"rel": ..., "to": "<id or code>"}}')


def normalize_diff(raw: Any) -> dict | None:
    """Файл для `end --diff` и `commit` в одной из трёх форм — к полному виду {"ops": [...]}.

    * список записей — ADD для каждой, порядок сохраняется;
    * одна запись (объект с `type` или `statement`, без `op`/`ops`) — один ADD;
    * полный дифф {"ops": [...]} — как есть; пустой объект (или только `diff_id`/`episode`) —
      пустой дифф, как раньше: `end` с итогом закрывает эпизод одним итогом.

    Всё прочее — ValidationError до любой записи. Форма проверяется и внутри операций
    (операция — объект с именем-строкой, запись — объект, доказательства и связи — списки),
    иначе `end` и `commit` упали бы на них трассировкой. Сами записи (тип, раздел, источники,
    секреты) проверяет общий путь записи."""
    if raw is None:
        return None
    if isinstance(raw, dict) and not raw.keys() - {"diff_id", "episode"}:
        raw = {"ops": [], **raw}
    if isinstance(raw, list):
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise S.ValidationError(f"diff item {i} is not an object; the diff file must be {DIFF_FORMS}")
            if "op" in item or "ops" in item:
                raise S.ValidationError(f"diff item {i} is an operation, not a record: a plain list holds records "
                                        'only; put operations into {"ops": [...]}')
        diff: dict = {"ops": [{"op": "ADD", "record": rec} for rec in raw]}
    elif isinstance(raw, dict) and "ops" in raw:
        stray = sorted({"op", "type", "statement", "record"} & raw.keys())
        if stray:
            raise S.ValidationError(f"the diff has both `ops` and record fields {stray}; the diff file must be "
                                    f"{DIFF_FORMS}")
        if not isinstance(raw["ops"], list):
            raise S.ValidationError("`ops` must be a list of operations")
        diff = raw
    elif isinstance(raw, dict) and "op" in raw:
        raise S.ValidationError('the diff is a single operation; wrap it as {"ops": [...]}')
    elif isinstance(raw, dict) and ("type" in raw or "statement" in raw):
        diff = {"ops": [{"op": "ADD", "record": raw}]}
    else:
        raise S.ValidationError(f"unrecognised diff ({type(raw).__name__}); the diff file must be {DIFF_FORMS}")
    for i, op in enumerate(diff["ops"]):
        if not isinstance(op, dict):
            raise S.ValidationError(f"op {i} must be an object")
        if not isinstance(op.get("op"), str):
            raise S.ValidationError(f"op {i}: `op` must be one of {OPS}")
        rec = op.get("record")
        if rec is not None and not isinstance(rec, dict):
            raise S.ValidationError(f"op {i}: `record` must be an object")
        _need_evidence_list(op.get("evidence"), f"op {i}: `evidence`")
        _need_evidence_list((rec or {}).get("evidence"), f"op {i}: record `evidence`")
        _need_links((rec or {}).get("links"), f"op {i}: record `links`")
    return diff


def apply_diff(mem, diff: dict, episode: str | None = None) -> dict:
    diff = normalize_diff(diff) or {}
    ops = diff.get("ops")
    if not isinstance(ops, list) or not ops:
        raise S.ValidationError("diff must have a non-empty `ops` list")
    from .memory import refuse_secrets

    refuse_secrets(diff, what="the knowledge diff")  # все поля: записи, доказательства, связи, причины
    episode = diff.get("episode") or episode
    store = mem.store
    diff_id, chash = diff_identity(diff, episode)

    def replay() -> dict | None:
        prior = store.applied_diff(diff_id)
        if not prior:
            return None
        if prior["content_hash"] != chash:
            raise S.ConflictError(f"diff_id {diff_id} was already applied with different content", [diff_id])
        result = json.loads(prior["result"])
        result["replayed"] = True  # повтор: ничего не записано повторно
        return result

    early = replay()
    if early:
        return early

    for op in ops:
        if not isinstance(op, dict):
            raise S.ValidationError("each op must be an object")
    ops = checks.prepare_ops(mem, ops)  # долгие проверки — до транзакции, вне блокировки общей базы
    refs: dict[str, str] = {}
    rep = _report()

    def rid_of(ref: str) -> str:
        if ref.startswith("@"):
            if ref[1:] not in refs:
                raise S.ValidationError(f"unknown diff-local ref {ref}")
            return refs[ref[1:]]
        return mem.resolve_target(ref)

    def record_of(ref: str) -> dict:
        rid = rid_of(ref)
        rec = store.get_record(rid)
        if not rec:
            raise S.NotFound(f"{ref} is not a memory record")
        return rec

    def check_version(rec: dict, op: dict, kind: str) -> None:
        expected = op.get("expected_version")
        if expected is None:
            raise S.ValidationError(
                f"{kind} {rec['id']} needs expected_version (the version you read; now v{rec['version']}). "
                "It prevents silently overwriting a change made by another session.")
        if int(expected) != rec["version"]:
            raise S.ConflictError(
                f"{rec['id']} changed since you read it: you have v{expected}, memory has v{rec['version']}. "
                "Re-read it with `get` and decide again.", [rec["id"]])

    def require_current(rec: dict) -> None:
        status = mem.status(rec)
        if status != "active":
            raise S.ValidationError(f"{rec['id']} is {status} in this worktree, not active")

    def add_links(rid: str, links: list[dict]) -> None:
        for ln in links or []:
            rel = ln.get("rel")
            if rel not in S.RELATIONS:
                raise S.ValidationError(f"relation {rel!r} unknown; use one of {S.RELATIONS}")
            origin = ln.get("origin", "asserted")
            if origin not in ("asserted", "inferred"):
                raise S.ValidationError("diff links are asserted or inferred; deterministic edges come from the indexer")
            target = rid_of(ln["to"])
            mem.refuse_ignored([mem._node_path(target)], "link target in")
            src, dst = (target, rid) if ln.get("reverse") else (rid, target)
            store.add_edge(src, rel, dst, origin, confidence=ln.get("confidence"), episode=episode,
                           note=ln.get("note"))
            rep["linked"].append({"from": src, "rel": rel, "to": dst, "origin": origin})

    def conflicts_for(fields: dict, exclude: str | None) -> list[str]:
        if not fields.get("subject") or fields["type"] not in S.CONTRADICTION_TYPES:
            return []
        rows = [r for r in store.records_by_subject(fields["subject"])
                if r["id"] != exclude and r["type"] in S.CONTRADICTION_TYPES]
        return [r["id"] for r in mem.current_records(rows)]

    def transition(rec: dict, kind: str, reason: str, by: str | None = None) -> None:
        """Перемена статуса: запись в журнал с коммитом и копией + глобальное поле для справки."""
        status = S.TRANSITION_OF[kind]
        # Перемена «на незакоммиченных правках» — только если изменены файлы самой записи (и замены).
        paths = set(mem.paths_of_record(rec["id"]))
        if by and kind == "SUPERSEDE":
            paths |= set(mem.paths_of_record(by))
        observed = mem.view.observed(sorted(paths))
        store.add_transition(rec["id"], status, observed, episode, by, reason)
        patch: dict[str, Any] = {"status": status, "valid_until": episode}
        if kind == "SUPERSEDE":
            patch["superseded_by"] = by
        store.update_record(rec["id"], patch, f"{kind}{' by ' + by if by and kind == 'SUPERSEDE' else ''}: "
                                              f"{reason}", episode)

    def insert_new(rec: dict, exclude_conflict: str | None) -> tuple[str, bool]:
        evs = mem._norm_evidence(rec.get("evidence"), episode)
        fields = mem._validate_new(rec, evs)
        dup = mem.find_duplicate(fields["type"], fields["statement"])
        if dup and dup != exclude_conflict:
            for ev in evs:
                store.add_evidence(dup, ev, episode)
            add_links(dup, rec.get("links") or [])  # связи дубля не теряются
            rep["duplicates_converted"].append({"statement": fields["statement"], "into": dup,
                                                "evidence_added": len(evs)})
            return dup, False
        if not rec.get("coexist"):
            clash = conflicts_for(fields, exclude_conflict)
            if clash:
                raise S.ConflictError(
                    f"subject {fields['subject']!r} already has active {clash} in this worktree. "
                    "Use SUPERSEDE (the old claim is no longer true), INVALIDATE, or coexist: true "
                    "(both hold at once).",
                    clash,
                )
        targets = []
        for ln in rec.get("links") or []:
            to = ln.get("to") or ""
            if not to.startswith("@"):
                try:
                    targets.append(mem.resolve_target(to, allow_create=False))
                except S.MemoryError_:
                    pass  # неверную цель отклонит add_links ниже, с понятной ошибкой
        rid = mem._insert(fields, evs, episode, mem.record_paths(evs, targets))
        for other, sim in mem.near_duplicates(fields["type"], fields["statement"], exclude=rid):
            rep["warnings"].append(f"{rid} is close to {other} (cos {sim}); consider merging by hand")
        add_links(rid, rec.get("links") or [])
        return rid, True

    with store.transaction():
        # Повторная проверка под блокировкой записи: два процесса с одним диффом не применят его дважды.
        late = replay()
        if late:
            return late
        for i, op in enumerate(ops):
            kind = (op.get("op") or "").upper()
            if kind not in OPS:
                raise S.ValidationError(f"op #{i}: {kind!r} unknown; use one of {OPS}")
            try:
                if kind == "ADD":
                    rid, created = insert_new(op.get("record") or {}, None)
                    if created:
                        rep["added"].append(rid)
                    if op.get("ref"):
                        refs[op["ref"]] = rid

                elif kind == "UPDATE":
                    rec = record_of(op["id"])
                    check_version(rec, op, kind)
                    patch = dict(op.get("patch") or {})
                    bad = set(patch) - PATCHABLE
                    if bad:
                        raise S.ValidationError(
                            f"cannot patch {sorted(bad)}: statement changes are SUPERSEDE, "
                            "status changes are INVALIDATE/RESOLVE/SUPERSEDE, type changes are CONFIRM promote_to"
                        )
                    if "section" in patch and patch["section"] not in S.SECTIONS:
                        raise S.ValidationError(f"section {patch['section']!r}: use one of {S.SECTIONS}")
                    if "reason" in patch:
                        from .memory import refuse_secrets

                        refuse_secrets(patch.get("reason"))
                    new_evs = mem._norm_evidence(op.get("evidence"), episode)
                    all_evs = store.evidence_for(rec["id"]) + new_evs
                    if "certainty" in patch or "confidence" in patch:
                        cert = patch.get("certainty")
                        if rec["type"] == "HYPOTHESIS" and cert in ("sourced", "attested"):
                            raise S.ValidationError("a HYPOTHESIS cannot be sourced/attested; use CONFIRM promote_to")
                        conf = patch.get("confidence")
                        if "certainty" in patch and cert is None:
                            raise S.ValidationError("certainty cannot be null")
                        mem.check_certainty(cert, float(conf) if conf is not None else None, all_evs)
                    if patch.get("subject") and patch["subject"] != rec.get("subject") and not op.get("coexist"):
                        clash = conflicts_for({"subject": patch["subject"], "type": rec["type"]}, rec["id"])
                        if clash:
                            raise S.ConflictError(f"subject {patch['subject']!r} already has active {clash}", clash)
                    for ev in new_evs:
                        store.add_evidence(rec["id"], ev, episode)
                    new = store.update_record(rec["id"], patch, f"UPDATE: {op.get('reason') or ''}".strip(), episode)
                    if "reason" in patch or "subject" in patch:
                        store.set_vector(rec["id"], to_blob(mem.embedder.embed(
                            f"{new['statement']} {new.get('reason') or ''} {new.get('subject') or ''}")))
                    rep["updated"].append(rec["id"])

                elif kind == "INVALIDATE":
                    rec = record_of(op["id"])
                    check_version(rec, op, kind)
                    require_current(rec)
                    if not op.get("reason"):
                        raise S.ValidationError("INVALIDATE needs reason")
                    for ev in mem._norm_evidence(op.get("evidence"), episode):
                        store.add_evidence(rec["id"], ev, episode)
                    transition(rec, kind, op["reason"])
                    rep["invalidated"].append(rec["id"])

                elif kind == "CONFIRM":
                    rec = record_of(op["id"])
                    evs = mem._norm_evidence(op.get("evidence"), episode)
                    if not evs:
                        raise S.ValidationError("CONFIRM needs evidence")
                    target_type = op.get("promote_to")
                    if target_type:
                        check_version(rec, op, "CONFIRM promote_to")
                    for ev in evs:
                        store.add_evidence(rec["id"], ev, episode)
                    cats = mem._cats(evs)
                    if cats & {"verifiable", "attested", "check"}:  # перепроверено: пометки этой копии снимаются
                        store.clear_stale(rid=rec["id"])
                    patch: dict[str, Any] = {}
                    passing = mem._passing_checks(evs)
                    if any(e["kind"] == "check" and not checks.state(mem, e)["satisfied"] for e in evs):
                        rep["warnings"].append(f"{rec['id']}: a check in CONFIRM did not pass; the record will "
                                               "show needs_recheck")
                    if target_type:
                        if rec["type"] not in PROMOTABLE_FROM or target_type not in PROMOTABLE_TO:
                            raise S.ValidationError(
                                f"promote_to: {rec['type']} -> {target_type} not allowed; "
                                f"from {sorted(PROMOTABLE_FROM)} to {sorted(PROMOTABLE_TO)}")
                        if not passing:
                            raise S.ValidationError("promotion needs a passing check (kind: check) that confirms "
                                                    "the hypothesis; a source alone is not proof")
                        if not op.get("coexist"):
                            clash = conflicts_for({"subject": rec.get("subject"), "type": target_type}, rec["id"])
                            if clash:
                                raise S.ConflictError(
                                    f"promoting {rec['id']} would contradict active {clash}; SUPERSEDE them "
                                    "first or pass coexist: true", clash)
                        patch.update(type=target_type, certainty=checks.best_level(mem, evs))
                    elif rec["type"] == "HYPOTHESIS":
                        if rec["certainty"] == "hypothesis":
                            patch["certainty"] = "probable"
                    elif passing:
                        patch["certainty"] = checks.best_level(mem, evs)
                    else:
                        level = mem.support_level(store.evidence_for(rec["id"]))
                        order = ["sourced", "attested", "probable", "hypothesis", "unknown"]
                        cur = rec["certainty"]
                        if cur in order and level in order and order.index(level) < order.index(cur):
                            patch["certainty"] = level
                    if patch:
                        store.update_record(rec["id"], patch, f"CONFIRM: {op.get('reason') or ''}".strip(), episode)
                    rep["confirmed"].append(rec["id"])

                elif kind == "SUPERSEDE":
                    old = record_of(op["id"])
                    check_version(old, op, kind)
                    require_current(old)
                    if not op.get("reason"):
                        raise S.ValidationError("SUPERSEDE needs reason")
                    new_rec = dict(op.get("record") or {})
                    new_rec.setdefault("subject", old.get("subject"))
                    new_rec.setdefault("type", old["type"])
                    if old.get("section"):
                        new_rec.setdefault("section", old["section"])
                    new_id, created = insert_new(new_rec, old["id"])
                    if not created:
                        raise S.ValidationError(f"replacement duplicates existing {new_id}; INVALIDATE {old['id']}")
                    transition(old, kind, op["reason"], by=new_id)
                    store.add_edge(new_id, "SUPERSEDES", old["id"], "asserted", episode=episode, note=op["reason"])
                    if op.get("inherit_links", True):
                        for e in store.edges_of(old["id"]):
                            if e["origin"] == "deterministic" or e["rel"] in NO_INHERIT:
                                continue
                            src = new_id if e["src"] == old["id"] else e["src"]
                            dst = new_id if e["dst"] == old["id"] else e["dst"]
                            if src != dst:
                                store.add_edge(src, e["rel"], dst, e["origin"], confidence=e["confidence"],
                                               episode=episode, note=f"inherited from {old['id']}")
                    rep["added"].append(new_id)
                    rep["superseded"].append({"old": old["id"], "new": new_id})
                    if op.get("ref"):
                        refs[op["ref"]] = new_id

                elif kind == "RESOLVE":
                    rec = record_of(op["id"])
                    check_version(rec, op, kind)
                    if rec["type"] not in S.RESOLVABLE:
                        raise S.ValidationError(f"{rec['type']} cannot be resolved; use INVALIDATE or SUPERSEDE")
                    require_current(rec)
                    for ev in mem._norm_evidence(op.get("evidence"), episode):
                        store.add_evidence(rec["id"], ev, episode)
                    by = rid_of(op["by"]) if op.get("by") else None
                    transition(rec, kind, op.get("reason") or "", by=by)
                    if by:
                        store.add_edge(by, "RESOLVES", rec["id"], "asserted", episode=episode)
                    rep["resolved"].append(rec["id"])

                elif kind == "LINK":
                    src = rid_of(op["from"])
                    tmp = {"rel": op.get("rel"), "to": op["to"], "origin": op.get("origin", "asserted"),
                           "confidence": op.get("confidence"), "note": op.get("note")}
                    add_links(src, [tmp])

                elif kind == "UNLINK":
                    src, dst = rid_of(op["from"]), rid_of(op["to"])
                    if not op.get("reason"):
                        raise S.ValidationError("UNLINK needs reason")
                    n = 0
                    for name in store.aliases(dst) if dst.startswith("code:") else [dst]:
                        for origin in ("asserted", "inferred"):
                            n += store.end_edges({"src": src, "rel": op["rel"], "dst": name, "origin": origin},
                                                 f"UNLINK: {op['reason']}")
                    if not n:
                        raise S.NotFound(f"no active non-deterministic edge {src} -{op['rel']}-> {dst}")
                    rep["unlinked"].append({"from": src, "rel": op["rel"], "to": dst})
            except (KeyError, TypeError) as exc:
                raise S.ValidationError(f"op #{i} ({kind}) is malformed: missing {exc}") from exc
            except S.MemoryError_ as exc:
                exc.args = (f"op #{i} ({kind}): {exc.args[0] if exc.args else exc}",)
                raise

        rep["diff_id"] = diff_id
        store.record_applied_diff(diff_id, chash, episode, rep)
        store.log_metric("commit", {k: len(v) for k, v in rep.items() if isinstance(v, list)}, episode)
    return rep
