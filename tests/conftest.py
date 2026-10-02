"""Общие заготовки: маленький git-репозиторий с кодом авторизации, пустая память, CLI в отдельном процессе."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory import Context, Memory
from am_helpers import git, write  # noqa: F401 — реэкспорт для фикстур

PKG_ROOT = Path(__file__).resolve().parents[1]

AUTH_SERVICE = '''
import redis


class AuthService:
    def __init__(self, client):
        self.client = client

    def rotate_token(self, user_id, old_token):
        new_token = self._issue(user_id)
        self.client.delete(old_token)
        self.client.set(new_token, user_id)
        return new_token

    def _issue(self, user_id):
        return f"tok-{user_id}"


def login(user_id):
    return AuthService(redis.Redis())._issue(user_id)
'''

REFRESH_CONTROLLER = '''
from app.auth.service import AuthService


def refresh(request):
    svc = AuthService(request.client)
    return svc.rotate_token(request.user_id, request.token)
'''

REFRESH_TEST = '''
from app.auth.service import AuthService


def test_refresh_race():
    svc = AuthService(None)
    assert svc.rotate_token
'''

# Тесты без внешних зависимостей: их память запускает сама в проверках kind=check.
SIMPLE_TEST = '''
def test_adds():
    assert 1 + 1 == 2


def test_broken():
    assert 1 + 1 == 3
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    write(root, "app/__init__.py", "")
    write(root, "app/auth/__init__.py", "")
    write(root, "app/auth/service.py", AUTH_SERVICE)
    write(root, "app/api/__init__.py", "")
    write(root, "app/api/refresh.py", REFRESH_CONTROLLER)
    write(root, "tests/test_refresh_race.py", REFRESH_TEST)
    write(root, "tests/test_simple.py", SIMPLE_TEST)
    write(root, "docs/auth.md", "# Auth\n\nRefresh tokens rotate on every use.\nOld token is revoked.\n")
    write(root, "README.md", "# Demo\n\nA tiny auth service.\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def mem(tmp_path: Path, repo: Path):
    m = Memory(tmp_path / "store", repo, roots=("app", "tests"))
    m.index()
    yield m
    m.close()


@pytest.fixture
def ctx(mem):
    c = Context(mem)
    c.start_session("test")
    return c


@pytest.fixture
def auth_source() -> str:
    return AUTH_SERVICE


@pytest.fixture
def put(repo: Path):
    """Переписать файл тестового репозитория: put("app/x.py", text)."""
    return lambda rel, text: write(repo, rel, text)


@pytest.fixture
def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


# Обычные тесты не опрашивают Windows (около двух секунд на start): CLI запускается обёрткой,
# которая внутри процесса отключает путь к powershell.exe. Владелец тогда unknown/no_interop —
# безопасное состояние. Переменной окружения для этого нет намеренно (агент не может так
# «отключить» проверку в настоящей работе). Тесты жизни владельца передают interop=True.
NO_INTEROP_BOOT = ("import sys; import agent_memory.session as s; s.WINDOWS_POWERSHELL = None; s.POSIX_PS = None; "
                   "from agent_memory.__main__ import main; sys.exit(main(sys.argv[1:]))")


@pytest.fixture(autouse=True)
def _no_windows_queries_in_process(monkeypatch):
    import agent_memory.session as session_mod

    monkeypatch.setattr(session_mod, "WINDOWS_POWERSHELL", None)
    monkeypatch.setattr(session_mod, "POSIX_PS", None)


class Cli:
    """agent_memory в отдельном процессе Python на каждый вызов — как у настоящего агента."""

    def __init__(self, store: Path, repo: Path, roots: str = "app,tests", interop: bool = False):
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AGENT_MEMORY_"))}
        self.env.update(AGENT_MEMORY_DIR=str(store), AGENT_MEMORY_REPO=str(repo), AGENT_MEMORY_ROOTS=roots,
                        PYTHONPATH=str(PKG_ROOT))
        self.session: str | None = None
        self.interop = interop

    def argv(self, *args: str) -> list[str]:
        if self.interop:
            return [sys.executable, "-m", "agent_memory", *args]
        return [sys.executable, "-c", NO_INTEROP_BOOT, *args]

    def run(self, *args: str, stdin: str | None = None, fault: str | None = None, session: bool = True,
            timeout: int = 120) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if fault:
            env["AGENT_MEMORY_FAULT"] = fault
        if session and self.session:
            env["AGENT_MEMORY_SESSION"] = self.session
        return subprocess.run(self.argv(*args), env=env, input=stdin,
                              capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)

    def json(self, *args: str, **kw) -> dict:
        out = self.run(*args, **kw)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def start(self) -> str:
        self.session = self.json("session", "start", session=False)["session"]
        return self.session


@pytest.fixture
def cli_factory(tmp_path: Path):
    return lambda repo, store=None, roots="app,tests", interop=False: Cli(store or tmp_path / "cli-store", repo,
                                                                        roots, interop)


@pytest.fixture
def two_worktrees(repo: Path, tmp_path: Path):
    """Две рабочие копии одного репозитория: A на main, B на ветке feature с изменённым кодом."""
    wt_b = tmp_path / "wt-b"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt_b))
    service = (wt_b / "app/auth/service.py").read_text(encoding="utf-8")
    # В ветке B функция переименована и сдвинута вниз: другие строки, другие связи.
    service = service.replace("    def rotate_token(", "    def rotate_refresh_token(").replace(
        "class AuthService:\n", "class AuthService:\n    VERSION = 2\n\n    def audit(self):\n        return 'b'\n\n")
    (wt_b / "app/auth/service.py").write_text(service, encoding="utf-8")
    ctrl = (wt_b / "app/api/refresh.py").read_text(encoding="utf-8").replace("rotate_token", "rotate_refresh_token")
    (wt_b / "app/api/refresh.py").write_text(ctrl, encoding="utf-8")
    git(wt_b, "commit", "-qam", "rename rotate_token in feature")
    return repo, wt_b
