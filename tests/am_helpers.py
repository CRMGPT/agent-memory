"""Помощники тестов памяти. Отдельный модуль с уникальным именем: `from conftest import ...`
в репозитории с двумя conftest.py мог бы взять чужой файл при полном прогоне."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return p


def git(root: Path, *args: str) -> str:
    # Переменные GIT_* из окружения не наследуем: с ними git пишет в чужой репозиторий.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "gc.auto=0",
                          *args], cwd=root, env=env, capture_output=True, text=True, check=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    return out.stdout.strip()
