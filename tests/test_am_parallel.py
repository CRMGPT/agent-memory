"""A и B: две сессии в двух рабочих копиях, каждая — отдельный процесс CLI, хранилище общее.

A — main, B — ветка feature, где rotate_token переименована и сдвинута. Проверяем, что
индексирование одной копии не меняет функции, строки, связи и оценку актуальности другой,
что эпизоды и заметки не смешиваются и что одновременные записи не теряются.
"""

from __future__ import annotations

import json
import subprocess
import time

ROTATE = "code:app/auth/service.py::AuthService.rotate_token"
RENAMED = "code:app/auth/service.py::AuthService.rotate_refresh_token"


def _line_of(path, needle):
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(needle)


def _decision(**kw):
    rec = {"type": "DECISION", "section": "logic", "statement": "rotate_token must delete the old token before storing the new one",
           "subject": "auth.rotation.order", "evidence": [{"kind": "code", "ref": ROTATE}],
           "links": [{"rel": "AFFECTS", "to": ROTATE}]}
    rec.update(kw)
    return {"ops": [{"op": "ADD", "record": rec}]}


def _snapshot_a(a, main):
    """Что видит A: свой код, свои строки, связи и актуальность записи."""
    pack = a.json("build", "refactor rotate_token", "--json")
    issue = a.json("build", "speed up AuthService._issue", "--json")
    src = a.json("sources", "decision_00001")
    svc = main / "app/auth/service.py"
    return {"injected": pack["injected"],
            "rotate_lines": src[0]["current_lines"] == f"app/auth/service.py:L{_line_of(svc, 'def rotate_token')}-L"
                                                      f"{_line_of(svc, 'return new_token')}",
            "issue_code": issue["code_injected"],
            "issue_lines": f"_issue — app/auth/service.py L{_line_of(svc, 'def _issue')}-" in issue["markdown"],
            "foreign": any(x in pack["markdown"] + issue["markdown"] for x in ("rotate_refresh_token", "VERSION")),
            "source": src[0]["verification"], "fresh": pack["code_freshness"]["fresh"]}


def test_two_worktrees_keep_their_own_code_state(two_worktrees, cli_factory, tmp_path):
    main, wt_b = two_worktrees
    store = tmp_path / "shared-store"
    a, b = cli_factory(main, store), cli_factory(wt_b, store)
    a.start()
    b.start()
    a.json("index")
    b.json("index")
    assert a.json("commit", json.dumps(_decision()))["added"] == ["decision_00001"]

    expected = {"injected": ["decision_00001"], "rotate_lines": True,
                "issue_code": ["code:app/auth/service.py::AuthService._issue"], "issue_lines": True,
                "foreign": False, "source": "unchanged", "fresh": True}
    assert _snapshot_a(a, main) == expected

    # B индексирует свою версию (в том числе с правкой без коммита) — у A ничего не меняется.
    svc_b = wt_b / "app/auth/service.py"
    svc_b.write_text(svc_b.read_text(encoding="utf-8") + "\n\ndef uncommitted_helper():\n    return 1\n",
                     encoding="utf-8")
    b.json("index")
    assert _snapshot_a(a, main) == expected

    # Глазами B: та же общая запись, но функции под ней в этой ветке нет — так и сказано.
    pack_b = b.json("build", "refactor rotate_refresh_token", "--json")
    assert RENAMED in pack_b["code_injected"] and ROTATE not in pack_b["code_injected"]
    assert b.json("sources", "decision_00001")[0]["verification"] == "missing"
    assert "uncommitted_helper" in json.dumps(b.json("related", "file:app/auth/service.py", "--depth", "1"))

    # Обратный порядок: A переиндексирует, потом B — результат A прежний, у B свой.
    a.json("index", "--full")
    b.json("index", "--full")
    assert _snapshot_a(a, main) == expected
    assert b.json("sources", "decision_00001")[0]["verification"] == "missing"

    # Правка в A без индекса: поиск честно говорит, что индекс устарел, а не показывает чужую ветку.
    svc_a = main / "app/auth/service.py"
    svc_a.write_text(svc_a.read_text(encoding="utf-8").replace("return new_token", "return new_token  # A"),
                     encoding="utf-8")
    stale = a.json("build", "refactor rotate_token", "--json")
    assert not stale["code_freshness"]["fresh"] and "CODE INDEX OUT OF DATE" in stale["markdown"]
    assert "app/auth/service.py" in stale["code_freshness"]["changed"]


def test_episodes_and_notes_do_not_mix(two_worktrees, cli_factory, tmp_path):
    main, wt_b = two_worktrees
    store = tmp_path / "shared-store"
    a, b = cli_factory(main, store), cli_factory(wt_b, store)
    a.start()
    b.start()
    assert a.run("begin", "task A").returncode == 0
    assert b.run("begin", "task B").returncode == 0
    a.json("note", "A-only note")
    b.json("note", "B-only note")
    b.json("note", "second B note")
    a.json("end", "--summary", "A done")
    archive = json.loads(next((store / "episodes").glob("*.json")).read_text(encoding="utf-8"))
    assert [n["text"] for n in archive["notes"]] == ["A-only note"]
    st = b.json("status")
    assert st["open_episode"]["task"] == "task B" and st["notes"] == 2
    # Сессия A не может писать в копию B, даже зная её путь.
    intruder = cli_factory(wt_b, store)
    intruder.session = a.session
    out = intruder.run("note", "sneaky")
    assert out.returncode == 4 and "does not own" in out.stderr


def test_concurrent_writes_keep_all_knowledge_and_detect_conflicts(two_worktrees, cli_factory, tmp_path):
    main, wt_b = two_worktrees
    store = tmp_path / "shared-store"
    a, b = cli_factory(main, store), cli_factory(wt_b, store)
    a.start()
    b.start()

    def spawn(cli, *args):
        env = dict(cli.env, AGENT_MEMORY_SESSION=cli.session)
        return subprocess.Popen(cli.argv(*args), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding="utf-8", errors="replace")

    def fact(statement):
        return json.dumps({"ops": [{"op": "ADD", "record": {
            "type": "FACT", "section": "code", "statement": statement, "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}}]})

    # Разные знания одновременно: оба сохраняются.
    procs = [spawn(a, "commit", fact(f"fact from A #{i}")) for i in range(3)] + \
            [spawn(b, "commit", fact(f"fact from B #{i}")) for i in range(3)]
    codes = [p.wait(60) for p in procs]
    assert codes == [0] * 6, [p.stderr.read() for p in procs]
    stmts = {h["statement"] for h in a.json("search", "fact from", "--limit", "20")}
    assert {f"fact from A #{i}" for i in range(3)} | {f"fact from B #{i}" for i in range(3)} <= stmts

    # Два обновления одной версии: одно проходит, второе — явный конфликт, не тихая перезапись.
    rid = a.json("search", "fact from A #0")[0]["id"]
    procs = [spawn(a, "update", rid, "--patch", '{"reason": "by A"}', "--expected-version", "1"),
             spawn(b, "update", rid, "--patch", '{"reason": "by B"}', "--expected-version", "1")]
    codes = sorted(p.wait(60) for p in procs)
    assert codes == [0, 3]
    loser = [p for p in procs if p.returncode == 3][0]
    assert "changed since you read it" in loser.stderr.read()
    rec = a.json("get", rid)
    assert rec["version"] == 2 and rec["reason"] in ("by A", "by B")

    # Повтор одного диффа (после таймаута) — без дублей, и при одновременном повторе тоже.
    pkg = json.dumps({"diff_id": "pkg-42", "ops": json.loads(fact("idempotent fact"))["ops"]})
    first = a.json("commit", pkg)
    assert first["added"] and not first.get("replayed")
    procs = [spawn(a, "commit", pkg), spawn(b, "commit", pkg)]
    assert [p.wait(60) for p in procs] == [0, 0]
    assert all(json.loads(p.stdout.read())["replayed"] for p in procs)
    assert len([h for h in a.json("search", "idempotent fact") if h["statement"] == "idempotent fact"]) == 1
    changed = json.dumps({"diff_id": "pkg-42", "ops": json.loads(fact("different content"))["ops"]})
    assert a.run("commit", changed).returncode == 3


def test_second_writer_refused_and_recovery_never_guesses_owner_death(repo, cli_factory, tmp_path):
    """Второй писатель получает отказ. Молчание владельца — не смерть: восстановление без
    доказательства отказано (жизнь владельца и передачу после его смерти проверяет
    test_am_reliability.py на настоящих процессах-держателях)."""
    store = tmp_path / "store"
    owner = cli_factory(repo, store)
    owner.start()
    assert owner.run("begin", "owner task").returncode == 0
    owner.json("note", "owner note")

    second = cli_factory(repo, store)
    out = second.run("note", "second writer")
    assert out.returncode == 4 and "held by session" in out.stderr
    out = second.run("session", "start", session=False)
    assert out.returncode == 4 and "One writer per worktree" in out.stderr
    out = second.run("session", "recover", session=False)
    assert out.returncode == 4 and "not proof" in out.stderr
    time.sleep(1)
    out = second.run("session", "recover", session=False)  # и после паузы — то же самое
    assert out.returncode == 4 and "unknown" in out.stderr
    assert owner.json("note", "owner still writes")["seq"] == 2
    owner.json("end", "--summary", "owner finished")
    archive = json.loads((store / "episodes" / "000001.json").read_text(encoding="utf-8"))
    assert [n["text"] for n in archive["notes"]] == ["owner note", "owner still writes"]


def test_uncommitted_knowledge_is_local_until_committed(two_worktrees, cli_factory, tmp_path):
    from am_helpers import git

    main, wt_b = two_worktrees
    store = tmp_path / "shared-store"
    a, b = cli_factory(main, store), cli_factory(wt_b, store)
    a.start()
    b.start()
    svc = wt_b / "app/auth/service.py"
    svc.write_text(svc.read_text(encoding="utf-8") + "\n# wip\n", encoding="utf-8")
    rid = b.json("commit", json.dumps({"ops": [{"op": "ADD", "record": {
        "type": "FACT", "section": "code", "statement": "service module ends with a wip marker",
        "evidence": [{"kind": "file", "ref": "app/auth/service.py"}]}}]}))["added"][0]
    assert b.json("get", rid)["applicability"] == "here"
    seen_by_a = a.json("get", rid)
    assert seen_by_a["applicability"] == "pending" and not seen_by_a["applies_here"]
    git(wt_b, "commit", "-qam", "wip marker")
    assert b.run("begin", "next task").returncode == 0  # пишущая команда привязывает работу к коммиту
    assert b.json("get", rid)["applicability"] == "history"
    assert a.json("get", rid)["applicability"] == "other"  # ветка B ещё не влита в main
    git(main, "merge", "-q", "--no-edit", "feature")
    assert a.json("get", rid)["applicability"] == "history"
