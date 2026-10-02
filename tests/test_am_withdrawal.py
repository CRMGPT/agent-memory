"""Уже показанная запись, которая перестала действовать (отозвана, заменена, другая ветка),
объявляется в следующей выдаче один раз; resume перепроверяет показанное."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import agent_memory.hook as hook
from agent_memory import Memory
from am_helpers import git

PKG_ROOT = Path(__file__).resolve().parents[1]


# ===================================== 2. отозванное после показа объявляется


def _hook_out(event: str, payload: dict) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        hook.run(event, payload)
    raw = buf.getvalue()
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw.strip() else ""


@pytest.fixture
def hook_env(monkeypatch, tmp_path):
    def setup(store: Path):
        monkeypatch.setenv("AGENT_MEMORY_DIR", str(store))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin data"))
    return setup


def test_invalidated_and_superseded_facts_are_announced_once(mem, ctx, hook_env):
    hook_env(mem.dir)
    a = mem.add({"type": "FACT", "section": "logic", "statement": "Refresh revokes the old token",
                 "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    b = mem.add({"type": "FACT", "section": "logic", "statement": "Access tokens live fifteen minutes",
                 "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    start = {"cwd": str(mem.root), "session_id": "w1", "source": "startup"}
    out = _hook_out("SessionStart", start)
    assert "Refresh revokes the old token" in out and "Access tokens live fifteen minutes" in out
    mem.invalidate(a, "revocation was removed", expected_version=mem.get(a)["version"])
    p = {"cwd": str(mem.root), "session_id": "w1", "prompt": "how is logging configured"}  # к a не относится
    out = _hook_out("UserPromptSubmit", p)
    assert f"WITHDRAWN {a}: invalidated - do not rely on it any more" in out
    assert "Refresh revokes the old token" not in out
    assert _hook_out("UserPromptSubmit", p) == ""  # объявлено один раз
    ctx.begin("token lifetime")
    ctx.end({"ops": [{"op": "SUPERSEDE", "id": b, "expected_version": mem.get(b)["version"], "reason": "changed",
                      "record": {"statement": "Access tokens live five minutes",
                                 "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}}]})
    q = {**p, "prompt": "how long do access tokens live"}
    out = _hook_out("UserPromptSubmit", q)
    assert f"WITHDRAWN {b}: superseded" in out and "Access tokens live five minutes" in out
    assert "WITHDRAWN" not in _hook_out("UserPromptSubmit", q)
    assert "WITHDRAWN" not in _hook_out("SessionStart", {**start, "session_id": "w2"})  # новая сессия: нечего отзывать


def test_fact_that_leaves_this_branch_is_announced_and_shown_again_when_back(repo, tmp_path, hook_env):
    git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    store = tmp_path / "store"
    mem = Memory(store, repo, roots=("app", "tests"))
    rid = mem.add({"type": "DECISION", "section": "code", "statement": "Rotation happens inside one transaction",
                   "evidence": [{"kind": "file", "ref": "app/auth/service.py"}]})["added"][0]
    mem.close()
    hook_env(store)
    p = {"cwd": str(repo), "session_id": "b1", "prompt": "rotation transaction"}
    assert "Rotation happens inside one transaction" in _hook_out("UserPromptSubmit", p)
    git(repo, "checkout", "-q", "-b", "older", "HEAD~1")  # коммит записи вне истории этой ветки
    out = _hook_out("UserPromptSubmit", p)
    assert f"WITHDRAWN {rid}: no longer applies here: recorded on another line of history (branch)" in out
    assert "Rotation happens inside one transaction" not in out
    assert _hook_out("UserPromptSubmit", p) == ""
    git(repo, "checkout", "-q", "main")
    assert "Rotation happens inside one transaction" in _hook_out("UserPromptSubmit", p)  # снова действует


def test_resume_rechecks_what_was_shown(mem, hook_env):
    hook_env(mem.dir)
    a = mem.add({"type": "FACT", "section": "data", "statement": "Exports run nightly",
                 "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]})["added"][0]
    assert "Exports run nightly" in _hook_out("UserPromptSubmit", {"cwd": str(mem.root), "session_id": "r1",
                                                                   "prompt": "exports nightly"})
    mem.invalidate(a, "moved to hourly", expected_version=mem.get(a)["version"])
    out = _hook_out("SessionStart", {"cwd": str(mem.root), "session_id": "r1", "source": "resume"})
    assert f"WITHDRAWN {a}: invalidated" in out
