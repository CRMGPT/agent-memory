"""Формы файла для `end --diff` и `commit`: список записей, одна запись, полный {"ops": [...]}.

Ошибка 02.10.2026: служебный блок хука описывал файл как набор записей, а `end` принимал только
{"ops": [...]} и на JSON-списке падал трассировкой AttributeError — агент, который следовал
инструкции, не мог сохранить знание. Теперь три понятные формы принимаются, всё прочее — отказ
с кодом 2 без трассировки и без частичного сохранения. Примеры из инструкций (хук, скилл, роль
исполнителя) проверяются настоящим `end`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import agent_memory.hook as hook
from agent_memory import schema as S
from agent_memory.diff import normalize_diff

PKG_ROOT = Path(__file__).resolve().parents[1]
SRC = "app/auth/service.py"


def _rec(statement: str, rtype: str = "FACT", section: str = "code", ref: str = SRC) -> dict:
    return {"type": rtype, "section": section, "statement": statement, "evidence": [{"kind": "file", "ref": ref}]}


def _started(repo, cli_factory, tmp_path):
    cli = cli_factory(repo, tmp_path / "store")
    cli.start()
    assert cli.run("begin", "save knowledge").returncode == 0
    return cli


def _end(cli, tmp_path, diff, *extra: str):
    path = tmp_path / "diff.json"
    path.write_text(json.dumps(diff, ensure_ascii=False), encoding="utf-8")
    return cli.run("end", "--diff", str(path), *extra)


def _statements(cli) -> list[str]:
    found = cli.json("search", "rotation token", "--limit", "50")
    return sorted(r["statement"] for r in found if r.get("type") != "EPISODE")


# ============================================================ принимаемые формы


def test_list_of_records_is_saved_as_one_add_per_record(repo, cli_factory, tmp_path):
    cli = _started(repo, cli_factory, tmp_path)
    out = _end(cli, tmp_path, [_rec("Token rotation deletes the old token"),
                               _rec("Token rotation is triggered by refresh", "DECISION", "logic")],
               "--summary", "rotation facts")
    assert out.returncode == 0, out.stderr
    assert "Traceback" not in out.stderr
    added = json.loads(out.stdout)["commit"]["added"]
    assert added[:2] == ["fact_00001", "decision_00001"]
    assert cli.json("get", "decision_00001")["section"] == "logic"
    assert cli.json("status")["open_episode"] is None


def test_single_record_is_saved_as_one_add(repo, cli_factory, tmp_path):
    cli = _started(repo, cli_factory, tmp_path)
    out = _end(cli, tmp_path, _rec("Token rotation deletes the old token"))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["commit"]["added"] == ["fact_00001"]
    assert cli.json("get", "fact_00001")["evidence"][0]["ref"] == SRC


def test_full_ops_form_still_works(repo, cli_factory, tmp_path):
    cli = _started(repo, cli_factory, tmp_path)
    out = _end(cli, tmp_path, {"ops": [{"op": "ADD", "record": _rec("Token rotation deletes the old token")}]})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["commit"]["added"] == ["fact_00001"]


def test_list_form_and_ops_form_are_the_same_diff():
    recs = [_rec("a statement"), _rec("b statement")]
    assert normalize_diff(recs) == {"ops": [{"op": "ADD", "record": r} for r in recs]}
    assert normalize_diff(recs[0]) == {"ops": [{"op": "ADD", "record": recs[0]}]}
    full = {"ops": [{"op": "ADD", "record": recs[0]}], "diff_id": "x"}
    assert normalize_diff(full) is full and normalize_diff(None) is None


def test_commit_accepts_the_list_form_too(repo, cli_factory, tmp_path):
    cli = cli_factory(repo, tmp_path / "store")
    cli.start()
    out = cli.run("commit", json.dumps([_rec("Token rotation deletes the old token")]))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["added"] == ["fact_00001"]


def test_repeat_of_the_same_list_end_replays_without_duplicates(repo, cli_factory, tmp_path):
    cli = _started(repo, cli_factory, tmp_path)
    diff = [_rec("Token rotation deletes the old token"), _rec("Token rotation stores the new token")]
    first = _end(cli, tmp_path, diff, "--summary", "rotation")
    assert first.returncode == 0, first.stderr
    again = _end(cli, tmp_path, diff, "--summary", "rotation")  # обрыв связи после записи, повтор
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout).get("replayed") is True
    assert _statements(cli) == ["Token rotation deletes the old token", "Token rotation stores the new token"]


# ============================================================ отказы без трассировки и без записи

BAD_FORMS = {
    "string": "just text",
    "number": 42,
    "list with a string": [_rec("Token rotation deletes the old token"), "oops"],
    "list with an operation": [{"op": "ADD", "record": _rec("Token rotation deletes the old token")}],
    "single operation": {"op": "ADD", "record": _rec("Token rotation deletes the old token")},
    "unknown object": {"facts": [_rec("Token rotation deletes the old token")]},
    "ops and record fields": {"ops": [], "type": "FACT", "statement": "x"},
    "ops is not a list": {"ops": {"op": "ADD"}},
    "op is not an object": {"ops": ["ADD"]},
    "record is not an object": {"ops": [{"op": "ADD", "record": "Token rotation deletes the old token"}]},
    "evidence is not a list": [{**_rec("Token rotation deletes the old token"), "evidence": "app/auth/service.py"}],
    "evidence item is a number": [{**_rec("Token rotation deletes the old token"), "evidence": [42]}],
    "op name is not a string": {"ops": [{"op": 5, "record": _rec("Token rotation deletes the old token")}]},
    "op without a name": {"ops": [{"record": _rec("Token rotation deletes the old token")}]},
    "links is not a list": [{**_rec("Token rotation deletes the old token"), "links": "abc"}],
    "link without a target": [{**_rec("Token rotation deletes the old token"), "links": [{"rel": "AFFECTS"}]}],
    "op evidence is not a list": {"ops": [{"op": "INVALIDATE", "id": "fact_00001", "evidence": "x"}]},
}


@pytest.mark.parametrize("name", sorted(BAD_FORMS))
def test_unrecognised_shapes_are_refused_cleanly(name, repo, cli_factory, tmp_path):
    cli = _started(repo, cli_factory, tmp_path)
    out = _end(cli, tmp_path, BAD_FORMS[name], "--summary", "must not be saved")
    assert out.returncode == 2, (out.returncode, out.stderr)
    assert "Traceback" not in out.stderr and out.stderr.startswith("ERROR (nothing saved):"), out.stderr
    assert cli.json("status")["open_episode"] is not None  # эпизод открыт, повтор возможен
    assert _statements(cli) == []
    with pytest.raises(S.ValidationError):
        normalize_diff(BAD_FORMS[name])


@pytest.mark.parametrize("name", sorted(BAD_FORMS))
def test_commit_refuses_the_same_shapes_without_a_traceback(name, repo, cli_factory, tmp_path):
    cli = cli_factory(repo, tmp_path / "store")
    cli.start()
    out = cli.run("commit", json.dumps(BAD_FORMS[name]))
    assert out.returncode == 2 and "Traceback" not in out.stderr, (out.returncode, out.stderr)
    assert _statements(cli) == []


@pytest.mark.parametrize("empty", [{}, {"diff_id": "only-an-id"}])
def test_empty_diff_with_a_summary_still_closes_the_episode(empty, repo, cli_factory, tmp_path):
    cli = _started(repo, cli_factory, tmp_path)
    out = _end(cli, tmp_path, empty, "--summary", "nothing new, episode closed with its summary")
    assert out.returncode == 0, out.stderr
    assert [r.split("_")[0] for r in json.loads(out.stdout)["commit"]["added"]] == ["episode"]
    assert cli.json("status")["open_episode"] is None


def test_kind_ref_string_evidence_still_works_on_every_write_path(repo, cli_factory, tmp_path):
    """Доказательство строкой `file:<путь>` принималось и раньше (Memory._norm_evidence) — не сужаем."""
    ev = [f"file:{SRC}"]
    cli = _started(repo, cli_factory, tmp_path)
    out = _end(cli, tmp_path, [{**_rec("Token rotation deletes the old token"), "evidence": ev}], "--summary", "s")
    assert out.returncode == 0, out.stderr
    assert cli.json("get", "fact_00001")["evidence"][0]["ref"] == SRC
    full = {"ops": [{"op": "ADD", "record": {**_rec("Token rotation stores the new token"), "evidence": ev}}]}
    assert cli.json("commit", json.dumps(full))["added"] == ["fact_00002"]
    assert cli.json("add", json.dumps({**_rec("Token rotation runs on refresh"), "evidence": ev}))["added"] == [
        "fact_00003"]
    version = cli.json("get", "fact_00003")["version"]
    upd = cli.run("update", "fact_00003", "--patch", json.dumps({"reason": "seen in the service"}),
                  "--expected-version", str(version), "--evidence", json.dumps(ev))
    assert upd.returncode == 0, upd.stderr


def test_one_bad_record_in_a_list_saves_nothing(repo, cli_factory, tmp_path):
    cli = _started(repo, cli_factory, tmp_path)
    no_section = {k: v for k, v in _rec("Token rotation stores the new token").items() if k != "section"}
    out = _end(cli, tmp_path, [_rec("Token rotation deletes the old token"), no_section])
    assert out.returncode == 2 and "section" in out.stderr and "Traceback" not in out.stderr, out.stderr
    no_source = {k: v for k, v in _rec("Token rotation stores the new token").items() if k != "evidence"}
    out = _end(cli, tmp_path, [_rec("Token rotation deletes the old token"), no_source])
    assert out.returncode == 2 and "evidence" in out.stderr and "Traceback" not in out.stderr, out.stderr
    assert _statements(cli) == []
    assert cli.json("status")["open_episode"] is not None


def test_secret_in_the_list_form_is_refused_on_end_and_commit(repo, cli_factory, tmp_path):
    secret = "pass" + "word=Zq9xLm2VbT"
    cli = _started(repo, cli_factory, tmp_path)
    out = _end(cli, tmp_path, [_rec("Token rotation deletes the old token"), _rec(f"the dev login is {secret}")])
    assert out.returncode == 2 and "secret" in out.stderr, out.stderr
    out = cli.run("commit", json.dumps([_rec(f"the dev login is {secret}")]))
    assert out.returncode == 2 and "secret" in out.stderr, out.stderr
    out = cli.run("commit", json.dumps(_rec(f"the dev login is {secret}")))
    assert out.returncode == 2 and "secret" in out.stderr, out.stderr
    assert _statements(cli) == []


def test_unreadable_diff_file_is_a_clean_refusal(repo, cli_factory, tmp_path):
    cli = _started(repo, cli_factory, tmp_path)
    (tmp_path / "a-dir").mkdir()
    for arg in (str(tmp_path / "a-dir"), str(tmp_path / "missing.json")):
        out = cli.run("end", "--diff", arg)
        assert out.returncode == 2 and "Traceback" not in out.stderr, out.stderr
    (tmp_path / "bad.json").write_bytes(b"\xff\xfe[")
    out = cli.run("end", "--diff", str(tmp_path / "bad.json"))
    assert out.returncode == 2 and "Traceback" not in out.stderr, out.stderr
    assert cli.json("status")["open_episode"] is not None


# ============================================================ примеры из инструкций работают как написаны


def _fill(example: str) -> object:
    """Подставить то, что агент заменяет сам: текст утверждения и путь к файлу."""
    text = example.replace('"..."', '"Token rotation deletes the old token"')
    text = text.replace("<path>", SRC).replace("path/to/file.py", SRC).replace("app/x.py", SRC)
    return json.loads(text)


def _hook_example() -> str:
    note = hook.integration_note("/proj", [("shell", "python3 /opt/agent-memory/bin/launcher.py cli")], "test")
    m = re.search(r"a list of records, for example (\[\{.*?\}\]\}\])", note)
    assert m, note
    return m.group(1)


def _skill_example() -> str:
    skill = (PKG_ROOT / "skills/project-memory/SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"is a list of new records.*?```json\n(.*?)```", skill, re.S)
    assert m, skill
    return m.group(1)


def _role_example(rel: str) -> str:
    role = (PKG_ROOT / rel).read_text(encoding="utf-8")
    m = re.search(r"`(\[\{\"type\".*?\}\]\}\])`", role)
    assert m, role
    return m.group(1)


def test_examples_in_hook_skill_and_role_are_accepted_by_end(repo, cli_factory, tmp_path):
    examples = {"hook": _hook_example(), "skill": _skill_example(),
                "executor role": _role_example("agents/executor.md")}
    for name, example in examples.items():
        diff = _fill(example)
        assert isinstance(diff, list), (name, diff)  # инструкции показывают короткую форму
        cli = cli_factory(repo, tmp_path / f"store-{name.replace(' ', '-')}")
        cli.start()
        assert cli.run("begin", "save knowledge").returncode == 0
        out = _end(cli, tmp_path, diff, "--summary", f"example from the {name}")
        assert out.returncode == 0, (name, out.stderr)
        assert len(json.loads(out.stdout)["commit"]["added"]) == 2, name  # запись + итог эпизода
