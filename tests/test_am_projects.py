"""Проекты и платформы: чья это память и где она лежит.

Хранилище нового проекта — в каталоге данных пользователя по устойчивому id (файл внутри общего
.git или явная метка без git), а не в рабочей копии. Worktree одного репозитория — одна память,
отдельные клоны — разные. Домашний каталог, корень диска, неопределённый проект и чужая
сторона (Windows/WSL) — записи нет, причина объясняется. Пути с пробелами и кириллицей.

Настоящие процессы и файловая система этой ОС; Windows-специфичные проверки идут при запуске
под Windows (нативно), POSIX-специфичные — под Linux/macOS.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import agent_memory.session as session_mod
from agent_memory.paths import ID_FILE, MARKER, resolve_project, side
from am_helpers import git, write

PKG_ROOT = Path(__file__).resolve().parents[1]
NO_INTEROP_BOOT = ("import sys; import agent_memory.session as s; s.WINDOWS_POWERSHELL = None; s.POSIX_PS = None; "
                   "from agent_memory.__main__ import main; sys.exit(main(sys.argv[1:]))")


def make_repo(path: Path, text: str = "def hello():\n    return 1\n") -> Path:
    write(path, "app/mod.py", text)
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def user_dirs(tmp_path, monkeypatch):
    """Отдельные «домашний каталог» и каталог данных пользователя для теста."""
    home, data = tmp_path / "дом пользователя", tmp_path / "данные"
    home.mkdir()
    data.mkdir()
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("LOCALAPPDATA", str(data))
    monkeypatch.delenv("AGENT_MEMORY_DIR", raising=False)
    monkeypatch.delenv("AGENT_MEMORY_REPO", raising=False)
    return home, data


def run_cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AGENT_MEMORY_"))}
    env.update(PYTHONPATH=str(PKG_ROOT), AGENT_MEMORY_ROOTS="app")
    return subprocess.run([sys.executable, "-c", NO_INTEROP_BOOT, *args], cwd=cwd, env=env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=120)


def cli_json(cwd: Path, *args: str) -> dict:
    out = run_cli(cwd, *args)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_worktrees_share_and_clones_do_not(tmp_path, user_dirs):
    main = make_repo(tmp_path / "проект с пробелом")
    wt = tmp_path / "worktree копия"
    git(main, "worktree", "add", "-q", "-b", "side", str(wt))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(main), str(clone))
    a = resolve_project(main, create=True)
    b = resolve_project(wt, create=True)
    c = resolve_project(clone, create=True)
    assert a.store and a.store == b.store and a.project_id == b.project_id
    assert c.store and c.store != a.store
    assert str(a.store).startswith(str(user_dirs[1]))  # не в рабочей копии
    assert not (main / ".agent-memory").exists() and (main / ".git" / ID_FILE).is_file()
    assert resolve_project(main / "app").store == a.store  # из подкаталога — тот же проект


def test_reader_mode_creates_nothing(tmp_path, user_dirs):
    repo = make_repo(tmp_path / "repo")
    proj = resolve_project(repo, create=False)
    assert proj.store is None and proj.reason is None  # памяти ещё нет: пусто, без ошибки
    assert not (repo / ".git" / ID_FILE).exists()


def test_two_projects_do_not_share_memory(tmp_path, user_dirs):
    one, two = make_repo(tmp_path / "один"), make_repo(tmp_path / "два")
    tok = cli_json(one, "session", "start")["session"]
    cli_json(one, "--session", tok, "commit", json.dumps({"ops": [{"op": "ADD", "record": {
        "type": "FACT", "section": "code", "statement": "project one hello returns 1",
        "evidence": [{"kind": "file", "ref": "app/mod.py"}]}}]}))
    assert cli_json(one, "search", "hello returns")[0]["statement"] == "project one hello returns 1"
    assert cli_json(two, "search", "hello returns") == []
    assert cli_json(one, "where")["store"] != cli_json(two, "where")["store"]


def test_no_git_needs_an_explicit_root_and_home_is_refused(tmp_path, user_dirs):
    home, _ = user_dirs
    plain = home / "заметки" / "проект"
    plain.mkdir(parents=True)
    proj = resolve_project(plain)
    assert proj.store is None and "not a project" in proj.reason
    out = run_cli(plain, "session", "start")
    assert out.returncode == 2 and "not a project" in out.stderr
    assert cli_json(plain, "init-project")["project_id"]
    assert (plain / MARKER).is_file() and resolve_project(plain).store
    (home / MARKER).write_text(json.dumps({"id": "x"}), encoding="utf-8")
    out = run_cli(home, "init-project")
    assert out.returncode == 2 and "home directory" in out.stderr
    other = home / "другой"
    other.mkdir()
    assert "home directory" in resolve_project(other).reason  # метка в домашнем каталоге не делает его проектом


def test_home_as_a_git_repository_is_not_a_project(tmp_path, user_dirs):
    home, _ = user_dirs
    make_repo(home)
    proj = resolve_project(home / "app", create=True)
    assert proj.store is None and "home directory" in proj.reason
    assert not (home / ".git" / ID_FILE).exists()


def test_legacy_store_next_to_git_is_kept(tmp_path, user_dirs):
    repo = make_repo(tmp_path / "legacy")
    (repo / ".agent-memory").mkdir()
    proj = resolve_project(repo, create=True)
    assert proj.legacy and proj.store == (repo / ".agent-memory").resolve()


def test_other_side_owns_the_project(tmp_path, user_dirs):
    repo = make_repo(tmp_path / "shared")
    other = "windows" if side() != "windows" else "wsl"
    (repo / ".git" / ID_FILE).write_text(json.dumps({"id": "abc", "side": other}), encoding="utf-8")
    proj = resolve_project(repo, create=True)
    assert proj.store is None and f"belongs to the {other} side" in proj.reason
    out = run_cli(repo, "session", "start")
    assert out.returncode == 2 and "one store, one side" in out.stderr


def test_unreadable_id_file_is_not_overwritten(tmp_path, user_dirs):
    repo = make_repo(tmp_path / "broken")
    (repo / ".git" / ID_FILE).write_text("{not json", encoding="utf-8")
    proj = resolve_project(repo, create=True)
    assert proj.store is None and "unreadable" in proj.reason
    assert (repo / ".git" / ID_FILE).read_text(encoding="utf-8") == "{not json"


def test_project_can_disable_the_integration(tmp_path, user_dirs):
    repo = make_repo(tmp_path / "off")
    (repo / ".agent-memory-off").write_text("", encoding="utf-8")
    assert resolve_project(repo).disabled


def test_full_cycle_in_a_cyrillic_path_with_spaces(tmp_path, user_dirs):
    repo = make_repo(tmp_path / "Проект Ёлка (тест)")
    tok = cli_json(repo, "session", "start")["session"]
    out = run_cli(repo, "--session", tok, "begin", "задача с кириллицей")
    assert out.returncode == 0, out.stderr
    cli_json(repo, "--session", tok, "note", "заметка")
    res = cli_json(repo, "--session", tok, "end", "--summary", "готово")
    assert res["saved"]
    assert cli_json(repo, "--session", tok, "session", "finish")["lease_released_now"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: a WSL path opened from native Windows")
def test_wsl_path_from_native_windows_is_refused():
    distro = os.environ.get("AGENT_MEMORY_TEST_WSL_DISTRO", "Ubuntu")
    unc = Path("\\\\wsl$\\" + distro + "\\tmp")
    try:
        reachable = unc.exists()
    except OSError:  # нет такого дистрибутива: Windows отвечает ошибкой сетевого имени
        reachable = False
    if not reachable:
        pytest.skip(f"WSL distribution {distro} is not reachable (set AGENT_MEMORY_TEST_WSL_DISTRO)")
    proj = resolve_project(unc)
    assert proj.store is None and "inside WSL" in proj.reason


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process table (ps)")
def test_posix_owner_is_the_claude_process_and_its_death_is_proven(tmp_path, monkeypatch):
    """Настоящие процессы: заменитель с именем `claude` (копия /bin/sh) — предок команды."""
    sh = shutil.which("sh")
    fake = tmp_path / "claude"
    shutil.copy(sh, fake)
    rec_file = tmp_path / "rec.json"
    code = (f"import sys, json; sys.path.insert(0, {str(PKG_ROOT)!r}); import agent_memory.session as s; "
            f"s.WINDOWS_POWERSHELL = None; c = s.capture_owner(); "
            f"open({str(rec_file)!r}, 'w').write(json.dumps({{'rec': c, 'state': s.host_status(c)['state']}}))")
    subprocess.run([str(fake), "-c", f'"{sys.executable}" -c "{code}"'], check=True, timeout=60)
    got = json.loads(rec_file.read_text())
    rec = got["rec"]
    assert rec["kind"] == "posix-host" and rec["name"] == "claude" and got["state"] == "alive"
    monkeypatch.setattr(session_mod, "WINDOWS_POWERSHELL", None)
    monkeypatch.setattr(session_mod, "POSIX_PS", "ps")
    assert session_mod.owner_state(rec)["code"] == "host_gone"  # заменитель завершился
    assert session_mod.owner_state({**rec, "computer": "elsewhere"})["code"] == "other_host"
    assert session_mod.owner_state({**rec, "boot": ""})["code"] == "incomplete_owner_record"
