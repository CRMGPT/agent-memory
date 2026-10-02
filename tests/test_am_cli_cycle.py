"""Полный цикл через CLI, каждый шаг — новый процесс. Это проверка инструмента, а не эксперимент с Claude.

задача A -> знание -> завершение -> новый процесс -> задача B -> знание и источник вернулись ->
решение заменено в ветке -> на main действует старое, в ветке новое -> после слияния новое везде.
"""

from __future__ import annotations

import json

DECISION = {"ops": [{"op": "ADD", "record": {
    "type": "DECISION", "section": "logic", "statement": "Token rotation deletes the old token, then stores the new one",
    "reason": "a stolen old token must stop working at once", "subject": "auth.rotation.order",
    "evidence": [{"kind": "code", "ref": "AuthService.rotate_token"},
                 {"kind": "check", "method": "pattern", "ref": "AuthService.rotate_token",
                  "pattern": r"delete\(old_token\)", "expect": "match", "condition": "old token is deleted"}],
    "links": [{"rel": "AFFECTS", "to": "AuthService.rotate_token"}]}}]}


def test_full_cycle_with_branch_scoped_replacement(repo, cli_factory, tmp_path):
    from am_helpers import git

    store = tmp_path / "store"
    a = cli_factory(repo, store)
    a.start()
    assert a.run("begin", "Task A: understand token rotation").returncode == 0
    a.json("note", "read rotate_token; it deletes then sets")
    end = a.json("end", "--diff", json.dumps(DECISION), "--summary", "rotation order recorded")
    assert end["commit"]["added"][0] == "decision_00001"

    # Новый процесс, новая задача другими словами: знание и его источник возвращаются.
    pack = a.run("begin", "Task B: add metrics to rotate_token")
    assert pack.returncode == 0
    assert "decision_00001" in pack.stdout and "check_passed" in pack.stdout and "old token is deleted" in pack.stdout
    assert " · verified · " not in pack.stdout  # шаблон в коде — не «проверено»
    src = a.json("sources", "decision_00001")
    assert src[0]["verification"] == "unchanged" and src[1]["check"]["passed_and_fresh"]
    a.json("end", "--summary", "metrics task scoped")

    # Ветка feature в отдельной рабочей копии меняет порядок и заменяет решение.
    wt = tmp_path / "wt-feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    svc = wt / "app/auth/service.py"
    svc.write_text(svc.read_text(encoding="utf-8").replace(
        "        self.client.delete(old_token)\n        self.client.set(new_token, user_id)\n",
        "        self.client.set(new_token, user_id)\n        self.client.delete(old_token)\n"), encoding="utf-8")
    git(wt, "commit", "-qam", "store new token first")
    b = cli_factory(wt, store)
    b.start()
    assert b.run("begin", "Task C: reorder rotation").returncode == 0
    version = b.json("get", "decision_00001")["version"]
    replace = {"ops": [{"op": "SUPERSEDE", "id": "decision_00001", "expected_version": version,
                        "reason": "new token must exist before the old one disappears",
                        "record": {"statement": "Token rotation stores the new token, then deletes the old one",
                                   "evidence": [{"kind": "code", "ref": "AuthService.rotate_token"}]}}]}
    b.json("end", "--diff", json.dumps(replace), "--summary", "rotation order replaced on feature")

    # В ветке: старое решение закрыто, действует новое.
    old_b, new_b = b.json("get", "decision_00001"), b.json("get", "decision_00002")
    assert old_b["status_here"] == "superseded" and new_b["status_here"] == "active" and new_b["applies_here"]
    pack_b = b.json("build", "change rotate_token", "--json")
    assert "decision_00002" in pack_b["injected"] and "decision_00001" not in pack_b["injected"]

    # На main ветка не влита: старое решение действует, новое не применяется как истина.
    old_a, new_a = a.json("get", "decision_00001"), a.json("get", "decision_00002")
    assert old_a["status_here"] == "active"
    assert new_a["applicability"] == "other" and not new_a["applies_here"]
    pack_a = a.json("build", "change rotate_token", "--json")
    assert pack_a["injected"][0] == "decision_00001"  # закреплено на коде main
    if "decision_00002" in pack_a["injected"]:  # показана — только с явной пометкой, что здесь не действует
        block = pack_a["markdown"].split("[decision_00002]", 1)[1].split("### [", 1)[0]
        assert "⚠ SCOPE: recorded on another line of history" in block

    # После слияния ветки в main замена действует и там.
    git(repo, "merge", "-q", "--no-edit", "feature")
    old_m, new_m = a.json("get", "decision_00001"), a.json("get", "decision_00002")
    assert old_m["status_here"] == "superseded" and new_m["applies_here"]
