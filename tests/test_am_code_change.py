"""Переименование, перенос, удаление и правка функции: связи памяти не теряются молча."""

from __future__ import annotations


ROTATE = "code:app/auth/service.py::AuthService.rotate_token"


def _decision(mem):
    return mem.add({"type": "DECISION", "section": "logic", "statement": "Rotation must be atomic",
                    "evidence": [{"kind": "code", "ref": ROTATE}],
                    "links": [{"rel": "AFFECTS", "to": ROTATE}]})["added"][0]


def test_rename_keeps_memory_attached(mem, ctx, put, auth_source):
    rid = _decision(mem)
    put("app/auth/service.py", auth_source.replace("def rotate_token", "def rotate_refresh_token"))
    rep = mem.index()
    new_id = "code:app/auth/service.py::AuthService.rotate_refresh_token"
    assert rep["moved"] == [{"from": ROTATE, "to": new_id}]
    assert mem.store.get_node(ROTATE)["status"] == "moved"
    affects = [e for e in mem.store.edges_of(rid) if e["rel"] == "AFFECTS"]
    assert [e["dst"] for e in affects] == [new_id]
    assert mem.get(rid)["stale_reason"] is None
    src = mem.sources(rid)[0]
    assert src["current_ref"] == new_id and src["verification"] == "unchanged"
    # Задача по новому имени находит решение.
    assert rid in ctx.build("optimize rotate_refresh_token")["injected"]


def test_move_to_another_file_is_detected(mem, put, auth_source):
    rid = _decision(mem)
    body = auth_source.split("    def rotate_token", 1)[1].split("    def _issue", 1)[0]
    put("app/auth/service.py", auth_source.replace("    def rotate_token" + body, ""))
    put("app/auth/rotation.py", "class Rotator:\n    def rotate_token" + body)
    rep = mem.index()
    assert rep["moved"] == [{"from": ROTATE, "to": "code:app/auth/rotation.py::Rotator.rotate_token"}]
    assert mem.get(rid)["status"] == "active"


def test_deleted_function_marks_memory_stale(mem, ctx, put, auth_source):
    rid = _decision(mem)
    body = auth_source.split("    def rotate_token", 1)[1].split("    def _issue", 1)[0]
    put("app/auth/service.py", auth_source.replace("    def rotate_token" + body, ""))
    rep = mem.index()
    assert ROTATE in rep["missing"] and rid in rep["stale_records"]
    rec = mem.get(rid)
    assert rec["status"] == "active" and "disappeared" in rec["stale_reason"]
    assert mem.sources(rid)[0]["verification"] == "missing"
    pack = ctx.build("Rotation must be atomic in auth")
    assert "STALE" in pack["markdown"]
    # Перепроверка снимает пометку, но только с новым доказательством.
    mem.commit({"ops": [{"op": "CONFIRM", "id": rid, "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}]})
    assert mem.get(rid)["stale_reason"] is None


def test_changed_body_flags_evidence(mem, put, auth_source):
    rid = _decision(mem)
    put("app/auth/service.py", auth_source.replace("        self.client.delete(old_token)\n", ""))
    rep = mem.index()
    assert ROTATE in rep["changed"] and rid in rep["stale_records"]
    assert mem.sources(rid)[0]["verification"] == "changed"


def test_file_line_evidence_detects_edits(mem, repo):
    rid = mem.add({"type": "FACT", "section": "code", "statement": "Old token is revoked",
                   "evidence": [{"kind": "doc", "ref": "docs/auth.md", "locator": "L3-L4"}]})["added"][0]
    assert mem.sources(rid)[0]["verification"] == "unchanged"
    (repo / "docs/auth.md").write_text("# Auth\n\nTokens never rotate.\n", encoding="utf-8")
    assert mem.sources(rid)[0]["verification"] == "changed"


def test_deterministic_edges_come_from_ast(mem):
    calls = {(e["src"], e["dst"]) for e in mem.store.edges_where(rel="CALLS", active=1)}
    assert ("code:app/api/refresh.py::refresh", ROTATE) in calls
    assert (ROTATE, "code:app/auth/service.py::AuthService._issue") in calls
    tested = {(e["src"], e["dst"]) for e in mem.store.edges_where(rel="TESTED_BY", active=1)}
    assert (ROTATE, "code:tests/test_refresh_race.py::test_refresh_race") not in tested  # атрибут, не вызов
    assert ("code:app/auth/service.py::AuthService", "code:tests/test_refresh_race.py::test_refresh_race") in tested
    imports = {(e["src"], e["dst"]) for e in mem.store.edges_where(rel="IMPORTS", active=1)}
    assert ("file:app/auth/service.py", "ext:redis") in imports
    assert ("file:app/api/refresh.py", "file:app/auth/service.py") in imports
    assert all(e["origin"] == "deterministic" for e in mem.store.edges_where(rel="CALLS"))


def test_unparseable_file_is_reported_and_retried(mem, put, auth_source):
    put("app/auth/service.py", auth_source + "\ndef broken(:\n")
    rep = mem.index()
    assert rep["parse_errors"] == ["app/auth/service.py"]
    assert mem.store.get_node(ROTATE)["status"] == "present"  # полусломанный файл не стирает узлы
    put("app/auth/service.py", auth_source)
    assert mem.index()["parse_errors"] == []
