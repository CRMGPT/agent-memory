"""Сводка метрик хранения и поиска.

Все размеры — приблизительная оценка «символы / 4», а не токены модели. Память не видит
переписку агента и не измеряет его настоящий контекст: в сводке есть только то, что она
сама вернула (наборы записей) и сама хранила (заметки, working.md). Реальные токены агента здесь
не появляются, пока их не измерили снаружи.
"""

from __future__ import annotations

from statistics import mean


def _agg(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "avg": round(mean(vals), 1), "max": max(vals), "min": min(vals)}


def summary(store) -> dict:
    packs = [m["data"] for m in store.metrics() if m["event"] in ("build", "expand")]
    ends = [m["data"] for m in store.metrics("end")]
    commits = [m["data"] for m in store.metrics("commit")]
    statuses = {r["status"]: r["n"] for r in store.conn.execute(
        "select status, count(*) n from records group by status").fetchall()}
    return {
        "note": "sizes are approximate (chars/4) for text memory returned or stored; "
                "the agent's real context and token use are NOT measured here",
        "records": {"total": store.count_records(), "by_last_global_status": statuses},
        "episodes": {r["status"]: r["n"] for r in store.conn.execute(
            "select status, count(*) n from episodes group by status").fetchall()},
        "retrievals": len(packs),
        "lazy_lookups": len(store.metrics("expand")),
        "pack_tokens_est": _agg([b.get("pack_tokens_est") for b in packs]),
        "nodes_considered": _agg([b.get("nodes_considered") for b in packs]),
        "nodes_injected": _agg([b.get("nodes_injected") for b in packs]),
        "graph_hops": _agg([b.get("graph_hops") for b in packs]),
        "retrieval_latency_ms": _agg([b.get("latency_ms") for b in packs]),
        "source_match_rate": _agg([b.get("source_match_rate") for b in packs]),
        "memory_commits": len(commits),
        "records_added": sum(c.get("added", 0) for c in commits),
        "duplicates_converted": sum(c.get("duplicates_converted", 0) for c in commits),
        "invalidations": sum(c.get("invalidated", 0) for c in commits),
        "supersessions": sum(c.get("superseded", 0) for c in commits),
        "episode_notes_tokens_est": _agg([e.get("notes_tokens_est") for e in ends]),
        "episode_packs_returned_tokens_est": _agg([e.get("packs_returned_tokens_est") for e in ends]),
        "working_tokens_est_after_end": _agg([e.get("working_tokens_est_after") for e in ends]),
    }
