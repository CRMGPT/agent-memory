"""Определения ролей для задач с памятью: опечатка в конфиге ловится локально, без сети и Claude.

Проверяется то, что Claude Code читает из frontmatter: имя, описание, модель и список
инструментов. Что определения действительно загружаются в сессии и какая модель фактически
запущена — проверяется в настоящей сессии.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[1] / "agents"
SKILL = AGENTS.parent / "skills" / "project-memory" / "SKILL.md"
EXPECTED = {"researcher": "sonnet", "executor": "opus", "reviewer": "opus"}  # в Claude Code: agent-memory:<имя>
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip()
    raise AssertionError(f"{path.name}: frontmatter is not closed with ---")


def tools(meta: dict[str, str]) -> set[str] | None:
    """None — поле tools не задано: агент наследует все инструменты сессии."""
    if "tools" not in meta:
        return None
    return {t.strip() for t in meta["tools"].split(",") if t.strip()}


@pytest.mark.parametrize("name,model", sorted(EXPECTED.items()))
def test_definition_has_name_description_and_model(name, model):
    path = AGENTS / f"{name}.md"
    meta = frontmatter(path)
    assert meta.get("name") == name
    assert len(meta.get("description", "")) > 40
    assert meta.get("model") == model
    body = path.read_text(encoding="utf-8").split("---", 2)[2]
    assert "project-memory" in body and len(body.strip()) > 200


def test_tool_limits_match_the_roles():
    researcher = tools(frontmatter(AGENTS / "researcher.md"))
    assert researcher is not None, "researcher must not inherit all tools"
    assert not researcher & (WRITE_TOOLS | {"Bash"})
    assert {"Read", "Grep", "Glob"} <= researcher

    reviewer = tools(frontmatter(AGENTS / "reviewer.md"))
    assert reviewer is not None, "reviewer must not inherit all tools"
    assert not reviewer & WRITE_TOOLS
    assert {"Read", "Grep", "Glob", "Bash"} <= reviewer

    executor = frontmatter(AGENTS / "executor.md")
    assert tools(executor) is None  # единственный писатель: все инструменты сессии


def test_agent_names_are_unique():
    names = [frontmatter(p).get("name") for p in sorted(AGENTS.glob("*.md")) if frontmatter(p)]
    assert len(names) == len(set(names)), names
    assert set(EXPECTED) <= set(names)


def test_only_the_executor_runs_the_memory_cli_and_finishes_the_task():
    """Команды памяти выполняет только исполнитель; координатор лишь передаёт запросы;
    подтверждение оператора — только от человека; конец задачи — `session finish`."""
    researcher = (AGENTS / "researcher.md").read_text(encoding="utf-8")
    assert "only the current executor runs it" in researcher
    executor = (AGENTS / "executor.md").read_text(encoding="utf-8")
    assert "session finish" in executor and "--operator-confirmed" in executor
    assert "session hold --session" not in executor  # держатель упразднён
    reviewer = (AGENTS / "reviewer.md").read_text(encoding="utf-8")
    assert "/tmp" in reviewer and "real stores" in reviewer
    skill = SKILL.read_text(encoding="utf-8")
    assert "session finish" in skill and "session hold --session" not in skill
