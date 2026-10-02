"""Наличие источника не означает истинность утверждения.

Три разных вопроса: источник найден, фрагмент совпадает, утверждение проверено. verified
даёт только проверка, которую выполнила сама память, и она устаревает вместе со своей целью.
"""

from __future__ import annotations

import pytest

from agent_memory import ValidationError

ROTATE = "code:app/auth/service.py::AuthService.rotate_token"


def _src(mem, rid, kind):
    return next(s for s in mem.sources(rid) if s["kind"] == kind)


def test_false_claim_citing_existing_readme_is_not_verified(mem):
    rid = mem.add({"type": "FACT", "section": "code", "statement": "The service is written in Rust",
                   "evidence": [{"kind": "doc", "ref": "README.md"}]})["added"][0]
    rec = mem.get(rid)
    assert rec["certainty_here"] == "sourced"  # «есть источник», но не «проверено»
    s = _src(mem, rid, "doc")
    assert s["available"] is True and s["match"] == "not_recorded"
    with pytest.raises(ValidationError, match="cannot be declared"):
        mem.add({"type": "FACT", "section": "code", "statement": "The service is written in Go", "certainty": "verified",
                 "evidence": [{"kind": "doc", "ref": "README.md"}]})
    with pytest.raises(ValidationError, match="did not confirm"):  # проверка опровергает неверное утверждение
        mem.add({"type": "FACT", "section": "code", "statement": "The service is written in Rust (checked)",
                 "evidence": [{"kind": "check", "method": "pattern", "ref": "README.md", "pattern": "Rust",
                               "expect": "match", "condition": "README says Rust"}]})


def test_test_file_reference_is_not_a_test_run(mem):
    rid = mem.add({"type": "FACT", "section": "code", "statement": "Addition works",
                   "evidence": [{"kind": "test", "ref": "tests/test_simple.py"}]})["added"][0]
    assert mem.get(rid)["certainty_here"] == "sourced"
    assert "NOT run" in _src(mem, rid, "test")["label"]

    run = {"kind": "check", "method": "pytest", "ref": "tests/test_simple.py::test_adds", "expect": "pass",
           "condition": "test_adds asserts 1 + 1 == 2"}
    rid2 = mem.add({"type": "FACT", "section": "code", "statement": "Addition of small integers works",
                    "evidence": [run]})["added"][0]
    rec = mem.get(rid2)
    assert rec["certainty_here"] == "verified"
    chk = _src(mem, rid2, "check")["check"]
    assert chk["result"] == "pass" and chk["condition"] == "test_adds asserts 1 + 1 == 2"
    assert chk["commit"] and chk["origin"].startswith("executed by agent_memory")
    ev = rec["evidence"][0]
    assert ev["check_output"] and ev["check_target_hash"] and ev["checked_at"]

    with pytest.raises(ValidationError, match="did not confirm"):
        mem.add({"type": "FACT", "section": "code", "statement": "Broken test passes",
                 "evidence": [{**run, "ref": "tests/test_simple.py::test_broken"}]})
    rid3 = mem.add({"type": "FACT", "section": "code", "statement": "test_broken currently fails",
                    "evidence": [{**run, "ref": "tests/test_simple.py::test_broken", "expect": "fail",
                                  "condition": "test_broken is red"}]})["added"][0]
    assert mem.get(rid3)["certainty_here"] == "check_passed"  # ожидаемое падение — не «проверено»
    with pytest.raises(ValidationError, match="one test node id"):  # общий прогон набора — не проверка утверждения
        mem.add({"type": "FACT", "section": "code", "statement": "everything works",
                 "evidence": [{**run, "ref": "tests/test_simple.py"}]})


def test_source_and_check_changes_are_visible(mem, put):
    doc = mem.add({"type": "FACT", "section": "code", "statement": "Old token is revoked",
                   "evidence": [{"kind": "doc", "ref": "docs/auth.md", "locator": "L4-L4"}]})["added"][0]
    pat = mem.add({"type": "FACT", "section": "code", "statement": "rotate_token deletes the old token",
                   "evidence": [{"kind": "check", "method": "pattern", "ref": ROTATE,
                                 "pattern": r"delete\(old_token\)", "expect": "match",
                                 "condition": "delete(old_token) is called"}]})["added"][0]
    tst = mem.add({"type": "FACT", "section": "code", "statement": "Small additions work",
                   "evidence": [{"kind": "check", "method": "pytest", "ref": "tests/test_simple.py::test_adds",
                                 "condition": "test_adds passes"}]})["added"][0]
    assert mem.get(pat)["certainty_here"] == "check_passed" and mem.get(tst)["certainty_here"] == "verified"
    assert _src(mem, doc, "doc")["match"] == "unchanged"

    put("docs/auth.md", "# Auth\n\nRefresh tokens rotate on every use.\nOld tokens live forever.\n")
    assert _src(mem, doc, "doc")["match"] == "changed"
    put("app/auth/service.py", (mem.root / "app/auth/service.py").read_text(encoding="utf-8").replace(
        "self.client.delete(old_token)", "self.client.unlink(old_token)"))
    mem.index()
    assert mem.get(pat)["certainty_here"] == "needs_recheck"
    assert _src(mem, pat, "check")["check"]["target"] == "changed"
    put("tests/test_simple.py", (mem.root / "tests/test_simple.py").read_text(encoding="utf-8") + "\n# edited\n")
    assert mem.get(tst)["certainty_here"] == "needs_recheck"  # тест изменился — прежний прогон не в счёт
    mem.commit({"ops": [{"op": "CONFIRM", "id": tst, "evidence": [
        {"kind": "check", "method": "pytest", "ref": "tests/test_simple.py::test_adds", "condition": "rerun"}]}]})
    assert mem.get(tst)["certainty_here"] == "verified"


def test_external_and_attested_sources_keep_honest_status(mem, ctx):
    rid = mem.add({"type": "FACT", "section": "code", "statement": "redis-py DEL returns the number of keys removed",
                   "evidence": [{"kind": "url", "ref": "https://redis.io/docs/latest/commands/del/",
                                 "quote": "Integer reply: the number of keys that were removed.",
                                 "accessed": "2026-10-01", "version": "Redis 7"}]})["added"][0]
    rec = mem.get(rid)
    assert rec["certainty_here"] == "attested"
    s = _src(mem, rid, "url")
    assert "not that our code works" in s["label"] and s["excerpt"].startswith("Integer reply")

    said = mem.add({"type": "CONSTRAINT", "section": "logic", "statement": "Never log refresh tokens",
                    "evidence": [{"kind": "user", "ref": "owner", "note": "do not log tokens anywhere"}]})["added"][0]
    assert mem.get(said)["certainty_here"] == "attested"
    assert "paraphrased by the model" in _src(mem, said, "user")["label"]

    ctx.begin("check gateway")
    note = ctx.note("curl -v https://gw/refresh -> HTTP 504 after 2.003 s", kind="tool_result")
    out = mem.add({"type": "OBSERVATION", "section": "workflow", "statement": "Gateway times out refresh after about 2 s",
                   "evidence": [{"kind": "tool", "ref": note["ref"]}]})["added"][0]
    s = _src(mem, out, "tool")
    assert "linked to the original output" in s["label"] and "504 after 2.003 s" in s["excerpt"]
    assert mem.get(out)["certainty_here"] == "attested"  # засвидетельствовано, не проверено памятью
    with pytest.raises(ValidationError, match="does not exist"):
        mem.add({"type": "OBSERVATION", "section": "workflow", "statement": "x", "evidence": [{"kind": "tool", "ref": "note:episode_000001#99"}]})
