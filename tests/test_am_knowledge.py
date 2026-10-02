"""Дифф знаний, противоречия, правила подкреплённости, атомарность."""

from __future__ import annotations

import json

import pytest

from agent_memory import ConflictError, ValidationError

ROTATE = "code:app/auth/service.py::AuthService.rotate_token"


def _fact(statement, subject=None, **kw):
    rec = {"type": "FACT", "section": "code", "statement": statement, "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}
    if subject:
        rec["subject"] = subject
    rec.update(kw)
    return {"op": "ADD", "record": rec}


def test_contradiction_needs_explicit_supersede_and_keeps_history(mem, ctx, head_sha):
    ctx.begin("document auth")
    ctx.end({"ops": [_fact("AuthService uses JWT access tokens", "auth.mechanism")]})
    ctx.begin("migrate auth")
    with pytest.raises(ConflictError) as err:
        mem.commit({"ops": [_fact("AuthService uses OAuth access tokens", "auth.mechanism")]},
                   episode="episode_000002")
    assert err.value.conflicting == ["fact_00001"]
    assert mem.store.count_records() == 1  # ничего не записалось молча

    with pytest.raises(ValidationError, match="expected_version"):
        ctx.end({"ops": [{"op": "SUPERSEDE", "id": "fact_00001", "reason": "migrated",
                          "record": {"statement": "AuthService uses OAuth access tokens",
                                     "evidence": [{"kind": "commit", "ref": head_sha}]}}]})
    ctx.end({"ops": [{"op": "SUPERSEDE", "id": "fact_00001", "expected_version": 1, "reason": "migrated to OAuth",
                      "record": {"statement": "AuthService uses OAuth access tokens",
                                 "evidence": [{"kind": "commit", "ref": head_sha}]}}]})
    old, new = mem.get("fact_00001"), mem.get("fact_00002")
    assert old["status_here"] == "superseded" and old["certainty_here"] == "superseded"
    assert old["superseded_by"] == "fact_00002"
    assert new["status_here"] == "active" and new["valid_from"] == "episode_000002"
    assert [r["id"] for r in mem.history("auth.mechanism")] == ["fact_00001", "fact_00002"]

    hits = [h["id"] for h in mem.search("which tokens does AuthService use")]
    assert "fact_00002" in hits and "fact_00001" not in hits
    hist = [h["id"] for h in mem.search("which tokens does AuthService use", include_history=True)]
    assert "fact_00001" in hist
    why = mem.why("fact_00001")
    assert why["superseded_by_chain"][0]["id"] == "fact_00002"
    assert why["transitions"][0]["kind"] == "superseded" and why["transitions"][0]["at_commit"] == head_sha
    assert [v["change"].split(":")[0] for v in why["versions"]] == ["created", "SUPERSEDE by fact_00002"]


def test_coexist_allows_compatible_claims_about_one_subject(mem):
    mem.commit({"ops": [_fact("Auth tokens expire after 15 minutes", "auth.tokens")]})
    rep = mem.commit({"ops": [_fact("Auth tokens are signed with RS256", "auth.tokens", coexist=True)]})
    assert rep["added"] == ["fact_00002"]


def test_knowledge_diff_commits_only_new_knowledge(mem, ctx):
    mem.commit({"ops": [_fact("Refresh tokens rotate on every use")]})
    before = mem.store.count_records()
    ctx.begin("debug failing integration test")
    for i in range(300):
        ctx.note(f"pytest -k refresh run {i}: AssertionError token mismatch " + "." * 300, kind="tool_result")
    res = ctx.end({"ops": [
        _fact("refresh tokens rotate on every use."),  # то же самое -> CONFIRM
        _fact("The integration test fixture reuses one Redis db across workers"),
        {"op": "ADD", "record": {"type": "HYPOTHESIS", "section": "logic",
                                 "statement": "Parallel pytest workers flush each other's keys"}},
    ]})
    commit = res["commit"]
    assert commit["added"] == ["fact_00002", "hypothesis_00001"]
    assert commit["duplicates_converted"][0]["into"] == "fact_00001"
    assert mem.store.count_records() == before + 2
    assert len(mem.get("fact_00001")["evidence"]) == 2
    assert all("AssertionError" not in h["statement"] for h in mem.search("pytest refresh AssertionError"))
    archive = json.loads((mem.dir / "episodes" / "000001.json").read_text(encoding="utf-8"))
    assert len(archive["notes"]) == 300
    assert res["working_tokens_est_after"] * 20 < res["notes_tokens_est"]


def test_diff_is_atomic(mem):
    with pytest.raises(ValidationError):
        mem.commit({"ops": [_fact("first valid fact"),
                            {"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": "no evidence here"}}]})
    assert mem.store.count_records() == 0
    assert mem.store.edges_where(rel="DISCOVERED_IN") == []


@pytest.mark.parametrize("record, message", [
    ({"type": "FACT", "section": "code", "statement": "x is y"}, "needs evidence"),
    ({"type": "FACT", "section": "code", "statement": "x is y", "evidence": [{"kind": "model", "ref": ""}]}, "HYPOTHESIS"),
    ({"type": "DECISION", "section": "logic", "statement": "use y", "certainty": "verified",
      "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}, "cannot be declared"),
    ({"type": "DECISION", "section": "logic", "statement": "use y", "certainty": "sourced",
      "evidence": [{"kind": "user", "ref": "owner", "note": "use y"}]}, "stronger than the evidence"),
    ({"type": "HYPOTHESIS", "section": "logic", "statement": "maybe y", "certainty": "sourced"}, "stays hypothesis"),
    ({"type": "OBSERVATION", "section": "workflow", "statement": "y seen", "confidence": 0.99,
      "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}, "fresh passing pytest check"),
    ({"type": "FACT", "section": "code", "statement": "z", "evidence": [{"kind": "file", "ref": "app/nope.py"}]}, "not found"),
    ({"type": "FACT", "section": "code", "statement": "z", "evidence": [{"kind": "doc", "ref": "docs/auth.md"}],
      "links": [{"rel": "AFFECTS", "to": "no_such_function"}]}, "not in this worktree's code index"),
    ({"type": "FACT", "section": "code", "statement": "w " * 400, "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}, "one claim"),
    ({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "user", "ref": "owner"}]}, "needs note"),
    ({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "user"}]}, "needs ref"),
    ({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "commit", "ref": "abc1234"}]}, "not found in git"),
    ({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "file", "ref": "../outside.txt"}]}, "not found"),
    ({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "episode", "ref": "episode_999999"}]},
     "does not exist"),
    ({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "url", "ref": "https://example.org/x"}]},
     "bare URL"),
    ({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "check", "method": "pattern", "ref": ROTATE,
                                                      "pattern": "nonexistent_call", "expect": "match",
                                                      "condition": "x"}]}, "did not confirm"),
])
def test_rules_reject_bad_records(mem, record, message):
    with pytest.raises(Exception) as err:
        mem.add(record)
    assert message in str(err.value)


def test_hypothesis_promotion_requires_a_passing_check(mem, head_sha):
    mem.add({"type": "HYPOTHESIS", "section": "logic", "statement": "rotate_token deletes the old token"})
    with pytest.raises(ValidationError, match="promote_to"):
        mem.update("hypothesis_00001", {"type": "FACT", "section": "code"}, expected_version=1)
    with pytest.raises(ValidationError, match="cannot be declared"):
        mem.update("hypothesis_00001", {"certainty": "verified"}, expected_version=1)
    with pytest.raises(ValidationError, match="SUPERSEDE"):
        mem.update("hypothesis_00001", {"statement": "something else"}, expected_version=1)
    with pytest.raises(ValidationError, match="passing check"):  # источник — не доказательство
        mem.commit({"ops": [{"op": "CONFIRM", "id": "hypothesis_00001", "promote_to": "FACT", "expected_version": 1,
                             "evidence": [{"kind": "code", "ref": ROTATE}]}]})
    with pytest.raises(ValidationError, match="not allowed"):
        mem.commit({"ops": [{"op": "CONFIRM", "id": "hypothesis_00001", "promote_to": "BANANA",
                             "expected_version": 1, "evidence": [{"kind": "code", "ref": ROTATE}]}]})
    check = {"kind": "check", "method": "pattern", "ref": ROTATE, "pattern": r"\.delete\(old_token\)",
             "expect": "match", "condition": "rotate_token calls delete(old_token)"}
    mem.commit({"ops": [{"op": "CONFIRM", "id": "hypothesis_00001", "promote_to": "FACT", "expected_version": 1,
                         "evidence": [check]}]})
    rec = mem.get("hypothesis_00001")
    assert rec["type"] == "FACT" and rec["certainty_here"] == "check_passed"  # шаблон — не прогон теста
    assert rec["id"] == "hypothesis_00001"  # идентификатор стабилен и после повышения


def test_invalidate_and_unlink_keep_history(mem):
    mem.add({"type": "HYPOTHESIS", "section": "logic", "statement": "Duplicate HTTP delivery causes the race",
             "links": [{"rel": "AFFECTS", "to": ROTATE}]})
    mem.invalidate("hypothesis_00001", "reproduced with a single client", expected_version=1,
                   evidence=[{"kind": "test", "ref": "tests/test_refresh_race.py"}])
    rec = mem.get("hypothesis_00001")
    assert rec["status_here"] == "invalidated" and rec["certainty_here"] == "contradicted"
    assert mem.search("duplicate HTTP delivery") == []
    mem.commit({"ops": [{"op": "UNLINK", "from": "hypothesis_00001", "rel": "AFFECTS", "to": ROTATE,
                         "reason": "hypothesis rejected"}]})
    ended = mem.store.edges_where(src="hypothesis_00001", rel="AFFECTS")
    assert len(ended) == 1 and ended[0]["active"] == 0 and "UNLINK" in ended[0]["note"]


def test_working_md_stays_small_and_drops_dead_references(mem, ctx):
    mem.add({"type": "HYPOTHESIS", "section": "logic", "statement": "cache is cold on deploy"})
    text = ctx.read_working().replace("# Active hypotheses\n", "# Active hypotheses\n- [hypothesis_00001] cold cache\n")
    ctx.write_working(text)
    with pytest.raises(ValidationError, match="limit"):
        ctx.write_working(text + "x" * 7000)
    mem.invalidate("hypothesis_00001", "warmup job exists", expected_version=1)
    rep = ctx.compact()
    assert rep["removed_lines"][0]["inactive"] == ["hypothesis_00001"]
    assert "hypothesis_00001" not in ctx.read_working()
    assert "hypothesis_00001" not in ctx.working_path.read_text(encoding="utf-8")  # файл — копия базы
