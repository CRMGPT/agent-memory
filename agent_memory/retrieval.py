"""Поиск в три стадии.

A. Прямой поиск: полнотекст (BM25) + векторное сходство + точные якоря
   (файлы и имена кода из рамки задачи). Одного сходства мало: запись,
   у которой доказательство лежит в файле задачи, поднимается якорем.
B. Расширение по графу от сильных находок на 1–2 шага. Веса отношений,
   затухание по шагам, ограничение ветвления, широкие узлы не раскрываются.
   Ограничения и решения, привязанные к коду задачи, закрепляются.
C. Источники: для выбранных записей и узлов кода — текущий текст из файлов
   (это делает context.py через Memory.sources и чтение узла).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import schema as S
from .taskframe import build_frame, frame_anchors, frame_terms
from .textindex import cosine, fts_query, from_blob, tokens
from .view import APPLIES

PIN_TYPES = {"CONSTRAINT", "DECISION", "BUG"}
PIN_RELS = {"AFFECTS", "CONSTRAINED_BY", "DECIDED_BY", "CAUSED_BY"}



def section_of(rec: dict) -> str:
    return rec.get("section") or S.UNCLASSIFIED

@dataclass
class Hit:
    id: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    hop: int = 0
    pinned: bool = False

    def bump(self, score: float, reason: str, hop: int = 0) -> None:
        if score > self.score:
            self.score = score
            self.hop = hop
        if reason not in self.reasons:
            self.reasons.append(reason)


@dataclass
class Selection:
    frame: dict
    records: list[Hit]
    code: list[Hit]
    stats: dict


class Retriever:
    def __init__(self, mem, k_seeds: int = 8, depth: int = 2, fanout: int = 8, broad_degree: int = 60,
                 min_seed: float = 0.15, min_score: float = 0.08, rel_cut: float = 0.35, max_records: int = 20,
                 min_keep: int = 8, sections: tuple[str, ...] | None = None):
        self.mem = mem
        self.sections = tuple(sections) if sections else None
        self.store = mem.store
        self.k_seeds = k_seeds
        self.depth = depth
        self.fanout = fanout
        self.broad_degree = broad_degree
        self.min_seed = min_seed
        self.min_score = min_score
        self.rel_cut = rel_cut
        self.max_records = max_records
        self.min_keep = min_keep
        self._info: dict[str, tuple[str, str]] = {}

    def info(self, rec: dict) -> tuple[str, str]:
        """(статус здесь, класс применимости) — с кэшем на один поиск."""
        if rec["id"] not in self._info:
            self._info[rec["id"]] = (self.mem.status(rec), self.mem.applicability(rec))
        return self._info[rec["id"]]

    def applies(self, rec: dict) -> bool:
        return self.info(rec)[1] in APPLIES

    # --- стадия A -------------------------------------------------------

    def _rank_weight(self, rec: dict) -> float:
        # Вес по подкреплённости СЕЙЧАС: устаревшая проверка не держит запись наверху.
        live = self.mem.certainty(rec, self.info(rec)[0])
        w = S.CERTAINTY_WEIGHT.get(live, 0.5) * S.TYPE_WEIGHT.get(rec["type"], 1.0)
        if rec.get("stale_reason"):
            w *= 0.8
        if not self.applies(rec):  # другая ветка или чужие незакоммиченные правки: показываем, но ниже
            w *= 0.5
        return w

    def _eligible(self, rec: dict | None, include_history: bool) -> bool:
        if not rec or (self.sections and section_of(rec) not in self.sections):
            return False
        return include_history or self.info(rec)[0] == "active"

    def _direct_hits(self, frame: dict, include_history: bool, exclude: set[str]) -> tuple[dict[str, Hit], int]:
        terms = frame_terms(frame)
        query_tokens = set(tokens(" ".join(terms)))
        hits: dict[str, Hit] = {}
        considered = 0

        lex = self.store.fts_records(fts_query(terms), limit=40)
        considered += len(lex)
        top = max((s for _, s in lex), default=0.0) or 1.0
        for rid, s in lex:
            rec = self.store.get_record(rid)
            if rid in exclude or not self._eligible(rec, include_history):
                continue
            matched = sorted(query_tokens & set(tokens(f"{rec['statement']} {rec.get('reason') or ''} "
                                                       f"{rec.get('subject') or ''}")))
            score = 0.55 * (s / top) * self._rank_weight(rec)
            hits.setdefault(rid, Hit(rid)).bump(score, f"keyword match: {', '.join(matched[:6]) or 'subject/tags'}")

        qvec = self.mem.embedder.embed(" ".join(terms) or frame.get("task", ""))
        for rid, blob in self.store.all_vectors():
            considered += 1
            if rid in exclude:
                continue
            sim = cosine(qvec, from_blob(blob))
            if sim < 0.2:
                continue
            rec = self.store.get_record(rid)
            if not self._eligible(rec, include_history):
                continue
            score = 0.45 * sim * self._rank_weight(rec)
            h = hits.setdefault(rid, Hit(rid))
            h.bump(h.score + score if h.score else score, f"semantic similarity {sim:.2f}")

        return hits, considered

    def anchor_nodes(self, frame: dict) -> dict[str, Hit]:
        out: dict[str, Hit] = {}
        for anchor in frame_anchors(frame):
            exact = self.store.find_nodes_by_label(anchor.removeprefix("file:"))
            if exact:
                for n in exact[:5]:
                    out.setdefault(n["id"], Hit(n["id"])).bump(1.0, f"task names {anchor}")
                continue
            toks = tokens(anchor)
            if not toks:
                continue
            q = " AND ".join(f'"{t}"' for t in toks)
            for nid, _ in self.store.fts_nodes(q, limit=3):
                node = self.store.get_node(nid)
                if node and node["kind"] != "record" and node["status"] == "present":
                    out.setdefault(nid, Hit(nid)).bump(0.6, f"task term {anchor} ~ {node['label']}")
        for system in frame.get("systems") or []:
            nid = f"ext:{system.lower()}"
            if self.store.get_node(nid):
                out.setdefault(nid, Hit(nid)).bump(0.5, f"task names system {system}")
        return out

    def direct(self, query: str | dict, limit: int = 10, include_history: bool = False) -> list[dict]:
        frame = query if isinstance(query, dict) else build_frame(query)
        hits, _ = self._direct_hits(frame, include_history, set())
        ranked = sorted(hits.values(), key=lambda h: -h.score)[:limit]
        out = []
        for h in ranked:
            rec = self.store.get_record(h.id)
            out.append({"id": h.id, "score": round(h.score, 3), "reasons": h.reasons, "type": rec["type"],
                        "section": section_of(rec),
                        "status": self.info(rec)[0], "applicability": self.info(rec)[1],
                        "certainty": self.mem.certainty(rec, self.info(rec)[0]), "version": rec["version"],
                        "statement": rec["statement"]})
        return out

    # --- стадия B -------------------------------------------------------

    def _expand(self, seeds: dict[str, Hit], include_history: bool) -> tuple[dict[str, Hit], int, int]:
        reached: dict[str, Hit] = {nid: Hit(nid, h.score, list(h.reasons), h.hop, h.pinned)
                                   for nid, h in seeds.items()}
        frontier = list(seeds)
        visited = set(frontier)
        max_hop = 0
        for hop in range(1, self.depth + 1):
            nxt: list[str] = []
            for nid in frontier:
                parent = reached[nid]
                if hop > 1 and self.store.degree(nid) > self.broad_degree:
                    continue
                node = self.store.get_node(nid)
                if node and node["kind"] == "record":
                    rec = self.store.get_record(nid)
                    if not self._eligible(rec, include_history):
                        continue
                edges = sorted(self.store.edges_of(nid), key=lambda e: -S.RELATION_WEIGHT.get(e["rel"], 0.3))
                taken = 0
                for e in edges:
                    if taken >= self.fanout:
                        break
                    other = e["dst"] if e["src"] == nid else e["src"]
                    onode = self.store.get_node(other)
                    if onode is None or onode["kind"] == "episode":
                        continue
                    if onode["kind"] == "record" and not self._eligible(self.store.get_record(other),
                                                                        include_history):
                        continue
                    w = S.RELATION_WEIGHT.get(e["rel"], 0.3) * (0.7 if e["origin"] == "inferred" else 1.0)
                    score = parent.score * w * 0.6
                    if score < self.min_score / 2:
                        continue
                    arrow = f"-{e['rel']}->" if e["src"] == nid else f"<-{e['rel']}-"
                    label = (self.store.get_node(nid) or {}).get("label", nid)
                    h = reached.setdefault(other, Hit(other))
                    h.bump(score, f"graph: {label[:60]} {arrow} ({e['origin']})", hop)
                    max_hop = max(max_hop, hop)
                    taken += 1
                    if other not in visited:
                        visited.add(other)
                        nxt.append(other)
            frontier = nxt
        return reached, len(visited), max_hop

    def _pin_constraints(self, code_hits: dict[str, Hit], records: dict[str, Hit], exclude: set[str]) -> None:
        """Ограничение на код задачи попадает в контекст, даже если в тексте задачи нет его слов."""
        for nid, h in code_hits.items():
            if h.hop > 1:
                continue
            for e in self.store.edges_of(nid):
                if e["rel"] not in PIN_RELS:
                    continue
                other = e["src"] if e["dst"] == nid else e["dst"]
                if other in exclude:
                    continue
                rec = self.store.get_record(other)
                if rec and rec["type"] in PIN_TYPES and self._eligible(rec, False) and self.applies(rec):
                    r = records.setdefault(other, Hit(other))
                    if r.pinned:
                        continue
                    reason = f"pinned: {rec['type'].lower()} {e['rel']} {nid}"
                    r.bump(max(r.score, 0.9), reason, 1)
                    r.reasons.remove(reason)
                    r.reasons.insert(0, reason)  # самая сильная причина печатается первой
                    r.pinned = True

    # --- целиком ---------------------------------------------------------

    def _spread_sections(self, chosen: list[Hit], ranked: list[Hit]) -> list[Hit]:
        """Без фильтра: из каждого раздела, где есть полезная (выше порога) находка, лучшая
        поднимается в начало набора, чтобы общий бюджет не съел целый раздел. Ничего не
        дублируется: это перестановка выбранного и добавление недостающего представителя."""
        def sec(h: Hit) -> str:
            return section_of(self.store.get_record(h.id) or {})

        head_n = min(3, len(chosen))
        head, tail = list(chosen[:head_n]), list(chosen[head_n:])
        have = {sec(h) for h in head}
        for name in S.SECTIONS:
            if name in have:
                continue
            best = next((h for h in ranked if sec(h) == name), None)
            if best is None:
                continue
            if best in tail:
                tail.remove(best)
            elif len(head) + len(tail) >= self.max_records and tail:
                tail.pop()
            head.append(best)
            have.add(name)
        return head + tail

    def retrieve(self, frame: dict, include_history: bool = False, exclude: set[str] | None = None) -> Selection:
        t0 = time.monotonic()
        exclude = exclude or set()
        direct, considered = self._direct_hits(frame, include_history, exclude)
        anchors = self.anchor_nodes(frame)
        seeds = {h.id: h for h in sorted(direct.values(), key=lambda h: -h.score)[: self.k_seeds]
                 if h.score >= self.min_seed}
        # Запись, привязанная к коду задачи (доказательством или смысловым ребром), — тоже семя.
        # Для файла смотрим все его сущности одним запросом: ограничение на функцию внутри
        # файла не должно зависеть от того, какие функции прошли лимит ветвления.
        for nid in list(anchors):
            node = self.store.get_node(nid) or {}
            links: list[tuple[str, str, str]] = []
            if node.get("kind") == "file" and node.get("path"):
                links = self.store.records_linked_to_file(node["path"])
            else:
                links = [(ev["record_id"], "EVIDENCE", nid) for ev in self.store.evidence_by_ref(nid)]
            for rid, rel, target in links:
                rec = self.store.get_record(rid)
                if not rec or rid in exclude or not self._eligible(rec, include_history):
                    continue
                h = direct.setdefault(rid, Hit(rid))
                label = target.split("::")[-1]
                if rel in PIN_RELS and rec["type"] in PIN_TYPES and self.applies(rec):
                    reason = f"pinned: {rec['type'].lower()} {rel} {label} in {node.get('label', nid)}"
                    h.bump(max(h.score, 0.9), reason)
                    h.reasons.remove(reason)
                    h.reasons.insert(0, reason)
                    h.pinned = True
                else:
                    h.bump(max(h.score, 0.7 * self._rank_weight(rec)), f"{rel.lower()} on {label}")
                seeds[rid] = h
        seeds.update(anchors)
        reached, visited, max_hop = self._expand(seeds, include_history)

        merged = dict(reached)
        for nid, h in direct.items():
            if nid in merged:
                for reason in h.reasons:
                    merged[nid].bump(h.score, reason)
                merged[nid].pinned |= h.pinned
            else:
                merged[nid] = h
        records: dict[str, Hit] = {}
        code: dict[str, Hit] = {}
        for nid, h in merged.items():
            node = self.store.get_node(nid)
            if not node or nid in exclude:
                continue
            if node["kind"] == "record":
                records[nid] = h
            elif node["kind"] != "episode":
                code[nid] = h
        self._pin_constraints(code, records, exclude)

        # Несколько лучших находок над абсолютным порогом берём всегда. Длинный хвост режем
        # относительно лучшей: запись слабее трети лидера — шум, даже если бюджет позволяет.
        # Закреплённые ограничения проходят всегда.
        pinned = sorted((h for h in records.values() if h.pinned), key=lambda h: -h.score)
        rest = sorted((h for h in records.values() if not h.pinned and h.score >= self.min_score),
                      key=lambda h: -h.score)
        cut = self.rel_cut * (rest[0].score if rest else 0.0)
        tail = [h for h in rest[self.min_keep:] if h.score >= cut]
        rec_list = pinned + (rest[: self.min_keep] + tail)[: max(0, self.max_records - len(pinned))]
        if not self.sections:
            rec_list = self._spread_sections(rec_list, rest)
        code_list = sorted((h for h in code.values() if h.score >= self.min_score), key=lambda h: -h.score)
        stats = {
            "nodes_considered": considered + visited,
            "graph_nodes_visited": visited,
            "graph_hops": max_hop,
            "seeds": len(seeds),
            "anchors": len(anchors),
            "records_candidates": len(records),
            "records_after_cutoff": len(rec_list),
            "code_candidates": len(code_list),
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        }
        return Selection(frame=frame, records=rec_list, code=code_list, stats=stats)
