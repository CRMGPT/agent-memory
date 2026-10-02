"""Обслуживание: находит проблемы, а меняет только безопасное."""

from __future__ import annotations

import os

import pytest

from agent_memory import ValidationError, maintenance

ROTATE = "code:app/auth/service.py::AuthService.rotate_token"


def test_maintenance_reports_without_rewriting(mem, put, auth_source):
    mem.commit({"ops": [
        {"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": "Auth tokens expire after 15 minutes",
                                 "subject": "auth.ttl", "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}},
        {"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": "Auth tokens are refreshed by the client",
                                 "subject": "auth.ttl", "coexist": True,
                                 "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}},
        {"op": "ADD", "record": {"type": "DECISION", "section": "logic", "statement": "Rotation must be atomic",
                                 "evidence": [{"kind": "code", "ref": ROTATE}],
                                 "links": [{"rel": "AFFECTS", "to": ROTATE}, {"rel": "RELATED_TO",
                                                                             "to": "concept:security"}]}},
    ]})
    mem.link("concept:unused", "RELATED_TO", "concept:also-unused")
    mem.commit({"ops": [{"op": "UNLINK", "from": "concept:unused", "rel": "RELATED_TO",
                         "to": "concept:also-unused", "reason": "test"}]})
    body = auth_source.split("    def rotate_token", 1)[1].split("    def _issue", 1)[0]
    put("app/auth/service.py", auth_source.replace("    def rotate_token" + body, ""))
    mem.index()

    versions_before = mem.store.conn.execute("select count(*) from record_versions").fetchone()[0]
    rep = maintenance.run(mem)
    assert rep["contradictions"][0]["subject"] == "auth.ttl"
    assert {"id": "decision_00001", "reason": mem.get("decision_00001")["stale_reason"]} in rep["stale"]
    assert any(p["id"] == "decision_00001" for p in rep["missing_provenance"])
    orphans = {o["id"] for o in rep["orphan_nodes"]}
    assert {"concept:unused", "concept:also-unused"} <= orphans
    assert mem.store.conn.execute("select count(*) from record_versions").fetchone()[0] == versions_before

    applied = maintenance.run(mem, apply_safe=True)["applied"]
    assert {"deleted_orphan": "concept:unused"} in applied
    assert mem.store.get_node("concept:unused") is None
    assert mem.store.get_node("concept:security") is not None  # связанный узел не трогаем
    assert mem.store.count_records() == 3  # записи не удаляются никогда


def test_episode_archive_is_read_only_and_export_is_idempotent(mem, ctx):
    from agent_memory.context import export_archive

    ctx.begin("t")
    ctx.end(None)
    path = mem.dir / "episodes" / "000001.json"
    assert not os.access(path, os.W_OK) or os.geteuid() == 0
    before = path.read_text(encoding="utf-8")
    export_archive(mem, mem.store.get_episode("episode_000001"))  # повторный экспорт ничего не меняет
    assert path.read_text(encoding="utf-8") == before
    assert list((mem.dir / "episodes").glob("*.orphan-*")) == []


def test_one_open_episode_at_a_time(ctx):
    ctx.begin("a")
    with pytest.raises(ValidationError, match="still open"):
        ctx.begin("b")
    dropped = ctx.drop_episode("wrong direction")
    assert dropped["episode"] == "episode_000001"
    assert ctx.begin("b")["episode"] == "episode_000002"


def test_parallel_worktrees_share_knowledge_not_working_state(tmp_path, repo):
    from agent_memory import Context, Memory

    ma = Memory(tmp_path / "store", repo, roots=("app", "tests"), scope="wt-a")
    mb = Memory(tmp_path / "store", repo, roots=("app", "tests"), scope="wt-b")
    a, b = Context(ma), Context(mb)
    a.start_session("a")
    b.start_session("b")
    a.begin("task in worktree a")
    b.begin("task in worktree b")  # вторая копия не блокируется чужим эпизодом
    a.write_working(a.read_working().replace("# Goal\n", "# Goal\nship A\n"))
    assert "ship A" not in b.read_working()
    a.end({"ops": [{"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": "Shared fact from worktree a",
                                            "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}}]})
    assert b.current()["task"] == "task in worktree b"
    assert "fact_00001" in b.expand("shared fact from worktree a")["injected"]
    ma.close()
    mb.close()
