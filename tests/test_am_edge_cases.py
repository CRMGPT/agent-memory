"""Регрессии: каждый тест воспроизводит найденный сценарий поломки."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_memory import ConflictError, ValidationError
from agent_memory.paths import store_dir

ROTATE = "code:app/auth/service.py::AuthService.rotate_token"


def _decision(statement="Rotation must be atomic"):
    return {"op": "ADD", "record": {"type": "DECISION", "section": "logic", "statement": statement,
                                    "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}}


def test_end_failure_writes_nothing_and_retry_is_clean(mem, ctx):
    ctx.begin("t")
    ctx.note("scratch")
    with pytest.raises(ValidationError, match="limit"):
        ctx.end({"ops": [_decision()]}, summary="s", working="x" * 7000)
    assert mem.store.count_records() == 0  # знание не записалось раньше проверки
    assert ctx.current()["id"] == "episode_000001" and len(mem.store.notes("episode_000001")) == 1
    res = ctx.end({"ops": [_decision()]}, summary="s")
    assert res["commit"]["added"] == ["decision_00001", "episode_summary_00001"]
    assert mem.get("decision_00001")["evidence"][0]["kind"] == "doc"
    assert len(mem.get("decision_00001")["evidence"]) == 1  # повтор не задвоил доказательства


def test_leftover_archive_file_does_not_block_end(mem, ctx):
    # Файл от сбоя старой версии: end не блокируется, файл откладывается рядом, ничего не удаляется.
    ctx.begin("t")
    (mem.dir / "episodes" / "000001.json").write_text("{}", encoding="utf-8")
    res = ctx.end({"ops": [_decision()]})
    assert res["saved"] and res["commit"]["added"] == ["decision_00001"]
    orphans = list((mem.dir / "episodes").glob("000001.orphan-*.json"))
    assert len(orphans) == 1 and orphans[0].read_text(encoding="utf-8") == "{}"
    assert '"decision_00001"' in (mem.dir / "episodes" / "000001.json").read_text(encoding="utf-8")


def test_opposite_claims_are_not_duplicates(mem):
    mem.add({"type": "FACT", "section": "code", "statement": "Retry delay must be > 5 s",
             "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})
    rep = mem.add({"type": "FACT", "section": "code", "statement": "Retry delay must be < 5 s",
                   "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})
    assert rep["added"] == ["fact_00002"] and rep["duplicates_converted"] == []


def test_duplicate_keeps_its_links(mem):
    mem.add({"type": "FACT", "section": "code", "statement": "rotate_token deletes before set",
             "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})
    rep = mem.add({"type": "FACT", "section": "code", "statement": "Rotate_token deletes before set.",
                   "evidence": [{"kind": "code", "ref": ROTATE}], "links": [{"rel": "AFFECTS", "to": ROTATE}]})
    assert rep["duplicates_converted"][0]["into"] == "fact_00001"
    assert rep["linked"] and rep["linked"][0]["from"] == "fact_00001"


def test_promotion_cannot_create_silent_contradiction(mem):
    mem.add({"type": "FACT", "section": "code", "statement": "cache ttl is 60", "subject": "cache.ttl",
             "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})
    mem.add({"type": "HYPOTHESIS", "section": "logic", "statement": "cache ttl is 300", "subject": "cache.ttl"})
    check = {"kind": "check", "method": "pattern", "ref": "docs/auth.md", "pattern": "rotate", "expect": "match",
             "condition": "docs mention rotation"}
    with pytest.raises(ConflictError):
        mem.commit({"ops": [{"op": "CONFIRM", "id": "hypothesis_00001", "promote_to": "FACT",
                             "expected_version": 1, "evidence": [check]}]})
    mem.add({"type": "FACT", "section": "code", "statement": "cache holds 10 items", "subject": "cache.size",
             "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})
    with pytest.raises(ConflictError):
        mem.update("fact_00002", {"subject": "cache.ttl"}, expected_version=1)  # смена subject тоже проверяется


def test_attested_evidence_stays_attested(mem):
    rid = mem.add({"type": "FACT", "section": "code", "statement": "Gateway timeout is 2 s",
                   "evidence": [{"kind": "tool", "ref": "curl -v", "note": "504 after 2.0s"}]})["added"][0]
    assert mem.get(rid)["certainty_here"] == "attested"


def test_file_evidence_cannot_escape_repo(mem, repo):
    (repo.parent / "secret.txt").write_text("TOKEN=abc\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="not found under repo root"):
        mem.add({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "file", "ref": "../secret.txt"}]})
    with pytest.raises(Exception):
        mem.resolve_target("../secret.txt")


def test_long_locator_is_compared_in_full(mem, put):
    put("docs/long.md", "\n".join(f"line {i}" for i in range(1, 121)) + "\n")
    rid = mem.add({"type": "FACT", "section": "code", "statement": "long doc lists lines",
                   "evidence": [{"kind": "doc", "ref": "docs/long.md", "locator": "L1-L90"}]})["added"][0]
    assert mem.sources(rid)[0]["verification"] == "unchanged"
    assert mem.sources(rid, max_lines=1)[0]["verification"] == "unchanged"
    assert mem.sources(rid, max_lines=1)[0]["excerpt"] == "line 1"


def test_tested_by_survives_reindex_of_implementation(mem, put, auth_source):
    def tested():
        return [e for e in mem.store.edges_where(rel="TESTED_BY", active=1)
                if e["dst"].endswith("test_refresh_race")]
    assert len(tested()) == 1
    put("app/auth/service.py", auth_source + "\n\ndef extra():\n    return 1\n")
    mem.index()
    assert len(tested()) == 1  # правка реализации не стирает связь, которой владеет тест


def test_branch_switch_restores_stale_without_touching_history(mem, put, auth_source):
    rid = mem.add({"type": "DECISION", "section": "logic", "statement": "Rotation must be atomic",
                   "evidence": [{"kind": "code", "ref": ROTATE}],
                   "links": [{"rel": "AFFECTS", "to": ROTATE}]})["added"][0]
    versions = len(mem.store.record_versions(rid))
    body = auth_source.split("    def rotate_token", 1)[1].split("    def _issue", 1)[0]
    put("app/auth/service.py", auth_source.replace("    def rotate_token" + body, ""))  # «другая ветка»
    mem.index()
    assert mem.get(rid)["stale_reason"]
    put("app/auth/service.py", auth_source)  # вернулись
    rep = mem.index()
    assert rep["restored"] >= 1 and mem.get(rid)["stale_reason"] is None
    assert len(mem.store.record_versions(rid)) == versions  # история записи не засорена


def test_trivial_bodies_are_not_renames(mem, put):
    put("app/stubs.py", "def alpha():\n    pass\n\n\ndef beta():\n    raise NotImplementedError\n")
    mem.index()
    put("app/stubs.py", "def gamma():\n    pass\n\n\ndef beta():\n    raise NotImplementedError\n")
    rep = mem.index()
    assert rep["moved"] == [] and "code:app/stubs.py::alpha" in rep["missing"]


def test_expand_does_not_repeat_pinned_constraint(mem, ctx):
    mem.add({"type": "CONSTRAINT", "section": "logic", "statement": "lock under 50 ms",
             "evidence": [{"kind": "user", "ref": "owner", "note": "gateway limit"}],
             "links": [{"rel": "AFFECTS", "to": ROTATE}]})
    assert "constraint_00001" in ctx.begin("refactor rotate_token")["injected"]
    assert "constraint_00001" not in ctx.expand("rotate_token lock")["injected"]


@pytest.mark.skipif(sys.platform == "win32", reason="//wsl$ links are translated on the WSL (POSIX) side only")
def test_store_dir_follows_windows_style_worktree_link(tmp_path: Path, monkeypatch):
    main = tmp_path / "main"
    (main / ".agent-memory").mkdir(parents=True)  # прежнее место памяти существует и сохраняется
    wt_meta = main / ".git" / "worktrees" / "wt1"
    wt_meta.mkdir(parents=True)
    (wt_meta / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "trees" / "wt1"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: //wsl$/Debian{wt_meta.as_posix()}\n", encoding="utf-8")
    monkeypatch.delenv("AGENT_MEMORY_DIR", raising=False)
    assert store_dir(wt) == (main / ".agent-memory").resolve()


def test_store_dir_refuses_to_guess(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_MEMORY_DIR", raising=False)
    from agent_memory.schema import MemoryError_

    with pytest.raises(MemoryError_, match="AGENT_MEMORY_DIR"):
        store_dir(tmp_path)


@pytest.mark.parametrize("value", [None, "contradicted", "superseded"])
def test_update_rejects_lifecycle_certainty(mem, value):
    mem.add({"type": "FACT", "section": "code", "statement": "q", "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})
    with pytest.raises(ValidationError, match="cannot be"):
        mem.update("fact_00001", {"certainty": value}, expected_version=1)


@pytest.mark.skipif(sys.platform == "win32", reason="//wsl$ links are translated on the WSL (POSIX) side only")
def test_commit_evidence_found_through_common_git_dir(mem, repo, head_sha):
    # Делаем из репозитория «дерево» с .git-файлом, указывающим на общий каталог по пути //wsl$.
    real = repo / ".git"
    common = repo.parent / "common.git"
    real.rename(common)
    (common / "worktrees" / "wt").mkdir(parents=True)
    (common / "worktrees" / "wt" / "commondir").write_text("../..\n", encoding="utf-8")
    (common / "worktrees" / "wt" / "HEAD").write_text(head_sha + "\n", encoding="utf-8")
    (common / "worktrees" / "wt" / "gitdir").write_text(f"{repo / '.git'}\n", encoding="utf-8")
    (repo / ".git").write_text(f"gitdir: //wsl$/Debian{(common / 'worktrees' / 'wt').as_posix()}\n",
                               encoding="utf-8")
    from agent_memory.memory import commit_exists

    assert commit_exists(repo, head_sha)
    assert not commit_exists(repo, "0" * 40)
