"""Вертикальный срез: задача -> старая память -> эпизод -> дифф -> коммит -> сброс -> новая задача -> вспомнили."""

from __future__ import annotations

import json

ROTATE = "code:app/auth/service.py::AuthService.rotate_token"


def test_episode_lifecycle_recovers_knowledge_after_drop(mem, ctx):
    # Эпизод 1: расследование. Временные заметки растут, в память уходит только дифф.
    first = ctx.begin("Fix concurrent refresh-token rotation in AuthService.rotate_token")
    assert first["episode"] == "episode_000001"
    assert "Relevant memory" not in first["markdown"]  # память пуста — вспоминать нечего
    for i in range(50):
        ctx.note(f"tool output #{i}: log line about request {i} " + "x" * 200, kind="tool_result")

    diff = {"ops": [
        {"op": "ADD", "ref": "race", "record": {
            "type": "BUG", "section": "code", "statement": "AuthService.rotate_token is not atomic: two concurrent refreshes "
                                        "can both succeed with the same old token",
            "subject": "auth.rotation.atomicity",
            "evidence": [{"kind": "test", "ref": "tests/test_refresh_race.py", "locator": "L1-L6",
                          "note": "reproduces duplicate token use"}],
            "links": [{"rel": "AFFECTS", "to": "AuthService.rotate_token"}]}},
        {"op": "ADD", "record": {
            "type": "DECISION", "section": "logic", "statement": "Refresh-token rotation must delete the old token before storing "
                                             "the new one, in one Redis transaction",
            "reason": "Concurrent refresh requests can otherwise reuse a token",
            "subject": "auth.rotation.design", "confidence": 0.9,
            "evidence": [{"kind": "code", "ref": ROTATE}, {"kind": "doc", "ref": "docs/auth.md", "locator": "L3-L4"},
                         {"kind": "check", "method": "pattern", "ref": ROTATE, "pattern": r"delete\(old_token\)",
                          "expect": "match", "condition": "rotate_token deletes the old token"}],
            "links": [{"rel": "AFFECTS", "to": ROTATE}, {"rel": "CAUSED_BY", "to": "@race"},
                      {"rel": "DEPENDS_ON", "to": "ext:redis"}]}},
        {"op": "ADD", "record": {
            "type": "HYPOTHESIS", "section": "logic", "statement": "Duplicate HTTP delivery from the mobile client causes the bug",
            "evidence": [{"kind": "model", "ref": "", "note": "guess from logs"}]}},
    ]}
    res = ctx.end(diff, summary="Investigated refresh race; found non-atomic rotate_token")
    assert res["saved"] and sorted(res["commit"]["added"]) == [
        "bug_00001", "decision_00001", "episode_summary_00001", "hypothesis_00001"]
    # Числа — приблизительная оценка (символы/4) заметок и рабочего состояния, не контекст агента.
    assert res["working_tokens_est_after"] < res["notes_tokens_est"] / 5
    assert mem.store.notes("episode_000001") == []  # заметки в архиве, в рабочей базе их нет
    archive = json.loads((mem.dir / "episodes" / "000001.json").read_text(encoding="utf-8"))
    assert len(archive["notes"]) == 50 and archive["diff"] == diff
    assert mem.get("bug_00001")["certainty_here"] == "sourced"  # файл теста есть, тест не запускали
    assert mem.get("decision_00001")["certainty_here"] == "check_passed"  # шаблон прошёл; это не прогон теста

    # Эпизод 2: новая задача другими словами. Знание возвращается из памяти, а не из истории чата.
    second = ctx.begin("Add metrics around rotate_token latency")
    md = second["markdown"]
    assert "decision_00001" in md and "bug_00001" in md
    assert "Why included" in md and "pinned" in md
    assert "tool output #" not in md
    assert "unchanged" in md
    assert "test was NOT run" in md
    assert md.count("def rotate_token") == 1
    why = mem.why("decision_00001")
    assert why["created_in_episode"]["id"] == "episode_000001"
    assert {s["kind"] for s in why["sources"]} == {"code", "doc", "check"}


def test_cli_end_to_end(cli_factory, repo):
    cli = cli_factory(repo)
    assert cli.run("index", session=False).returncode == 4  # без сессии писать нельзя
    out = cli.run("begin", "fix rotate_token race", "--frame", '{"systems": ["redis"]}', session=False)
    assert out.returncode == 0, out.stderr
    assert "episode_000001" in out.stdout
    cli.session = out.stdout.split("session ", 1)[1].split()[0]  # первый begin сам взял аренду
    note = cli.json("note", "pytest output: 1 failed")
    assert note["ref"] == "note:episode_000001#1"
    diff = {"ops": [{"op": "ADD", "record": {
        "type": "FACT", "section": "code", "statement": "rotate_token deletes the old token before storing the new one",
        "evidence": [{"kind": "code", "ref": "AuthService.rotate_token"}],
        "links": [{"rel": "AFFECTS", "to": "AuthService.rotate_token"}]}}]}
    end = cli.run("end", "--diff", "-", "--summary", "found ordering", stdin=json.dumps(diff))
    assert end.returncode == 0, end.stderr
    assert json.loads(end.stdout)["commit"]["added"][0] == "fact_00001"

    again = cli.run("begin", "change token storage in AuthService")
    assert again.returncode == 0 and "fact_00001" in again.stdout
    bad = cli.run("add", json.dumps({"type": "FACT", "section": "code", "statement": "Redis is fast"}))
    assert bad.returncode == 2 and "needs evidence" in bad.stderr and "nothing saved" in bad.stderr
    stats = cli.json("stats")
    assert stats["memory_commits"] == 1 and stats["retrievals"] == 2
    assert "NOT measured" in stats["note"]
