"""Обслуживание памяти: найти проблемы, не переписывая память.

Отчёт перечисляет дубли, противоречия, устаревшее, сироты графа, широкие узлы, записи
без доказательств и записи, которые давно никому не нужны. Всё — глазами этой рабочей
копии. Сам по себе отчёт ничего не меняет. `apply_safe=True` делает только то, что не
теряет знания: ставит и снимает пометки «устарело» этой копии по пропавшим источникам
и удаляет узлы-понятия без единой связи. Всё остальное агент решает диффом.
"""

from __future__ import annotations

import json

from . import schema as S
from .textindex import cosine, fts_query, normalize_statement

BROAD_DEGREE = 60
LOW_VALUE_AGE = 50  # эпизодов без единой выдачи
SOURCES_GONE = "all source files/entities of this record are gone in this worktree"


def run(mem, apply_safe: bool = False) -> dict:
    store = mem.store
    rep: dict = {"worktree": mem.scope, "duplicates": [], "near_duplicates": [], "contradictions": [],
                 "stale": [], "missing_provenance": [], "orphan_nodes": [], "broad_nodes": [], "low_value": [],
                 "applied": []}
    rows = list(store.iter_records())
    active = mem.current_records(rows)

    seen: dict[tuple[str, str], str] = {}
    for r in active:
        key = (r["type"], normalize_statement(r["statement"]))
        if key in seen:
            rep["duplicates"].append([seen[key], r["id"]])
        else:
            seen[key] = r["id"]
    active_ids = {r["id"] for r in active}
    pairs = set()
    for r in active:
        vec = store.get_vector(r["id"])
        if vec is None:
            continue
        for other_id, _ in store.fts_records(fts_query([r["statement"]]), limit=8):
            if other_id == r["id"] or other_id not in active_ids or (other_id, r["id"]) in pairs:
                continue
            other = store.get_record(other_id)
            ov = store.get_vector(other_id)
            if other and ov is not None and other["type"] == r["type"]:
                sim = cosine(vec, ov)
                if sim >= 0.92 and normalize_statement(other["statement"]) != normalize_statement(r["statement"]):
                    pairs.add((r["id"], other_id))
                    rep["near_duplicates"].append({"a": r["id"], "b": other_id, "cos": round(sim, 3)})

    by_subject: dict[str, list[str]] = {}
    for r in active:
        if r.get("subject") and r["type"] in S.CONTRADICTION_TYPES:
            by_subject.setdefault(r["subject"], []).append(r["id"])
    for subj, ids in by_subject.items():
        if len(ids) > 1:
            rep["contradictions"].append({"subject": subj, "active": ids,
                                          "note": "added with coexist; confirm both still hold"})
    for e in store.edges_where(rel="CONTRADICTS", active=1):
        if e["src"] in active_ids and e["dst"] in active_ids:
            rep["contradictions"].append({"edge": [e["src"], e["dst"]], "note": "both still active"})

    for r in active:
        evs = store.evidence_for(r["id"])
        if r["type"] in S.NEEDS_EVIDENCE and not evs:
            rep["missing_provenance"].append({"id": r["id"], "problem": "no evidence"})
        elif evs:
            srcs = mem._sources(r["id"], max_lines=0)
            checkable = [s for s in srcs if s["available"] is not None]
            gone = bool(checkable) and all(s["available"] is False for s in checkable)
            if gone:
                rep["missing_provenance"].append({"id": r["id"], "problem": SOURCES_GONE})
                if apply_safe and store.mark_stale(r["id"], "", SOURCES_GONE):
                    rep["applied"].append({"stale": r["id"]})
            elif apply_safe and SOURCES_GONE in (store.stale_reasons(r["id"]) or []):
                store.c.conn.execute("delete from stale_marks where record_id=? and node_id='' and reason=?",
                                     (r["id"], SOURCES_GONE))  # источники вернулись — пометка снимается сама
                rep["applied"].append({"unstale": r["id"]})
        reason = store.get_record(r["id"])["stale_reason"]
        if reason:
            rep["stale"].append({"id": r["id"], "reason": reason})

    for row in store.k.conn.execute("select id, kind from nodes where kind in ('concept', 'external')").fetchall():
        nid, kind = row["id"], row["kind"]
        deg = store.degree(nid)
        if deg == 0:
            rep["orphan_nodes"].append({"id": nid, "kind": kind})
            if apply_safe and not store.evidence_by_ref(nid):
                store.k.delete_node(nid)
                rep["applied"].append({"deleted_orphan": nid})
        elif deg > BROAD_DEGREE:
            rep["broad_nodes"].append({"id": nid, "kind": kind, "degree": deg})

    episodes = store.k.conn.execute("select id, injected from episodes order by id").fetchall()
    order = {row["id"]: i for i, row in enumerate(episodes)}
    used = set()
    for row in episodes:
        used.update(json.loads(row["injected"] or "[]"))
    for r in active:
        if r["type"] not in ("HYPOTHESIS", "OBSERVATION") or r["id"] in used:
            continue
        age = len(episodes) - order.get(r.get("created_from") or "", len(episodes))
        if age >= LOW_VALUE_AGE:
            rep["low_value"].append({"id": r["id"], "age_episodes": age})

    with store.transaction():
        store.log_metric("maintain", {k: len(v) for k, v in rep.items() if isinstance(v, list)})
    return rep
