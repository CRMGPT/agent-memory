"""Взгляд рабочей копии: где мы в истории git и что из общей памяти здесь действует.

Общая запись о коде не становится истиной для всех веток. У каждой записи и каждой
перемены её статуса (SUPERSEDE / INVALIDATE / RESOLVE) хранится, где она сделана:
коммит, ветка, рабочая копия и — если были незакоммиченные правки — их отпечаток:
какие отслеживаемые файлы изменены и каким было их содержимое.

Классы применимости в рабочей копии W:
  history   — коммит записи входит в историю HEAD копии W;
  here      — сделано в W на незакоммиченных правках, коммит записи в истории W,
              и эти правки (то же содержимое тех же файлов) всё ещё в дереве W;
  none      — вне git: действует везде;
  withdrawn — сделано в W на правках, которых в дереве больше нет (откатили, спрятали,
              переключили ветку): здесь не действует;
  pending   — сделано на незакоммиченных правках ДРУГОЙ копии: здесь не действует, пока
              именно это содержимое не придёт в историю W коммитом (тогда — history);
  other     — коммит не входит в историю W (другая ветка): здесь не действует.

Перемена статуса, сделанная в ветке B, не закрывает запись на main, пока B не влита.
Запись на незакоммиченных правках привязывается к коммиту, только когда именно эти
версии файлов закоммичены: дерево чистое, а содержимое файлов из отпечатка совпадает.

«Незакоммиченная правка своего файла» — содержимое файла на диске не равно его версии
в HEAD: изменённый отслеживаемый файл, НОВЫЙ неотслеживаемый файл или удалённый файл.
Считаются только собственные файлы записи, поэтому посторонние черновики ничего не держат.
Ничего не добавляется в индекс git: содержимое сравнивается через `hash-object` без `-w`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from .paths import git_base

APPLIES = ("here", "history", "none")


def _clean_env() -> dict[str, str]:
    # GIT_OPTIONAL_LOCKS=0: `git status` не обновляет индекс репозитория (чтение не пишет в .git).
    return {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_OPTIONAL_LOCKS": "0"}


def _load_patch(patch) -> dict | None:
    if patch is None or patch == "":
        return None
    if isinstance(patch, dict):
        return patch
    try:
        return json.loads(patch)
    except (TypeError, ValueError):
        return None


def _derived_file(rel: str, exclude: tuple[str, ...]) -> bool:
    """Производные файлы запуска (байткод Python, кэш pytest) и каталог памяти — не код."""
    parts = rel.split("/")
    if "__pycache__" in parts or ".pytest_cache" in parts or rel.endswith((".pyc", ".pyo")):
        return True
    return any(rel == d or rel.startswith(d + "/") for d in exclude)


class CodeView:
    def __init__(self, root: Path, scope: str, exclude: tuple[str, ...] = ()):
        self.root = Path(root)
        self.scope = scope
        self.exclude = tuple(e.strip("/") for e in exclude if e)
        self._base: list[str] | None | bool = False
        self.refresh()

    def _git(self, *args: str, timeout: int = 30) -> str | None:
        if self._base is False:
            self._base = git_base(self.root)
        if not self._base:
            return None
        try:
            out = subprocess.run([*self._base, *args], capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                                 env=_clean_env(), stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout if out.returncode == 0 else None

    def refresh(self) -> None:
        """Сбросить кэш: после коммита, переключения ветки или правок. Git спрашивается лениво."""
        self._head: str | None | bool = False
        self._branch: str | None | bool = False
        self._patch: dict | None = None
        self._untracked: list[str] | None = None
        self._status_ok = True
        self._history: set[str] | None = None
        self._hashes: dict[str, str] = {}
        self._blobs: dict[str, str | None] = {}

    @property
    def is_git(self) -> bool:
        return self.head is not None

    @property
    def head(self) -> str | None:
        if self._head is False:
            out = self._git("rev-parse", "HEAD")
            self._head = out.strip() if out else None
        return self._head  # type: ignore[return-value]

    @property
    def branch(self) -> str | None:
        if self._branch is False:
            b = self._git("rev-parse", "--abbrev-ref", "HEAD")
            b = b.strip() if b else None
            self._branch = None if b in (None, "HEAD") else b
        return self._branch  # type: ignore[return-value]

    def file_hash(self, rel: str) -> str:
        """Содержимое файла рабочей копии сейчас: sha1 или 'deleted'."""
        if rel not in self._hashes:
            p = self.root / rel
            self._hashes[rel] = hashlib.sha1(p.read_bytes()).hexdigest() if p.is_file() else "deleted"
        return self._hashes[rel]

    def _status(self) -> None:
        """Один `git status`: правки отслеживаемых файлов и неотслеживаемые (не игнорируемые) файлы."""
        tracked: list[str] = []
        untracked: list[str] = []
        out = self._git("status", "--porcelain", "-z", "--untracked-files=all") if self.head else ""
        self._status_ok = out is not None
        parts = (out or "").split("\0")
        i = 0
        while i < len(parts):
            entry = parts[i]
            if len(entry) > 3:
                if entry[:2] == "??":
                    untracked.append(entry[3:])
                else:
                    tracked.append(entry[3:])
                    if entry[0] in "RC":  # переименование: следующий элемент — прежний путь
                        tracked.append(parts[i + 1])
                        i += 1
            i += 1
        self._patch = {p: self.file_hash(p) for p in sorted(set(tracked))}
        self._untracked = sorted(p for p in set(untracked) if not _derived_file(p, self.exclude))

    def patch(self) -> dict[str, str]:
        """Отпечаток незакоммиченных правок отслеживаемых файлов: путь -> содержимое сейчас.

        Неотслеживаемые файлы здесь не считаются: забытый черновик иначе держал бы «грязным»
        всё дерево и мешал привязке записей к коммиту. Новые файлы самой записи учитывает
        own_changes, а состояние кода для проверок — code_state.
        """
        if self._patch is None:
            self._status()
        return self._patch  # type: ignore[return-value]

    def code_state(self) -> dict[str, str] | None:
        """Состояние кода для проверок: правки отслеживаемых файлов И содержимое неотслеживаемых.

        Новый локальный файл (тест, модуль) влияет на результат проверки так же, как правка
        отслеживаемого. Игнорируемые git файлы и производные (байткод, кэш pytest, каталог
        памяти) не считаются. None — git не ответил: состояние не подтверждено.
        """
        if self._patch is None:
            self._status()
        if not self._status_ok:
            return None
        state = dict(self._patch or {})
        for p in self._untracked or []:
            state[p] = self.file_hash(p)
        return state

    @property
    def dirty(self) -> bool:
        return bool(self.patch())

    def fingerprint(self) -> dict:
        """Точное состояние кода этой копии: HEAD плюс незакоммиченные правки и новые файлы."""
        return {"head": self.head, "state": self.code_state()}

    def blob_at(self, commit: str, rel: str) -> str | None:
        """Идентификатор содержимого файла в коммите (None — файла там нет)."""
        key = f"{commit}:{rel}"
        if key not in self._blobs:
            out = self._git("rev-parse", "--verify", "--quiet", key)
            self._blobs[key] = out.strip() if out else None
        return self._blobs[key]

    def blob_now(self, rel: str) -> str | None:
        """Идентификатор текущего содержимого файла на диске (None — файла нет)."""
        key = f"@work:{rel}"
        if key not in self._blobs:
            p = self.root / rel
            out = self._git("hash-object", "--", str(p)) if p.is_file() else None
            self._blobs[key] = out.strip() if out else None
        return self._blobs[key]

    def ignored(self, paths) -> list[str]:
        """Какие из путей git игнорирует (.gitignore и т.п.). Отслеживаемые файлы не игнорируются."""
        paths = sorted({p for p in paths or [] if p})
        if not paths or not self.head:
            return []
        if self._base is False:
            self._base = git_base(self.root)
        try:
            out = subprocess.run([*self._base, "check-ignore", "-z", "--stdin"], input="\0".join(paths) + "\0",
                                 capture_output=True, encoding="utf-8", errors="replace", timeout=30, env=_clean_env())
        except (OSError, subprocess.TimeoutExpired):
            return []
        if out.returncode != 0:  # 1 — ничего не игнорируется; 128 — ошибка git
            return []
        return sorted({p for p in out.stdout.split("\0") if p})

    def own_changes(self, paths) -> list[str]:
        """Какие из файлов записи сейчас отличаются от HEAD: изменены, новые или удалены.

        Сравнивается содержимое на диске с версией в HEAD, а не вывод `git status`, поэтому
        новый неотслеживаемый файл записи тоже считается правкой, а посторонние — нет.
        Игнорируемые git файлы не хэшируются и не учитываются (их не принимают и как источник).
        """
        if not self.head:
            return []
        candidates = set(paths or []) - set(self.ignored(paths))
        return sorted(p for p in candidates
                      if (self.blob_now(p) or "deleted") != (self.blob_at(self.head, p) or "deleted"))

    def changes_present(self, blobs: dict[str, str]) -> bool:
        """На диске ровно то содержимое файлов, о котором запись (каждый файл — тот же blob)."""
        return bool(blobs) and all((self.blob_now(p) or "deleted") == b for p, b in blobs.items())

    def _committed_blob(self, commit: str, path: str, blob: str) -> bool:
        key = f"{commit}..{self.head}:{path}={blob}"
        if key not in self._blobs:
            out = self._git("log", "--format=%H", f"{commit}..HEAD", "--", path) or ""
            self._blobs[key] = "yes" if any((self.blob_at(c, path) or "deleted") == blob for c in out.split()) else None
        return self._blobs[key] == "yes"

    def changes_committed(self, commit: str, blobs: dict[str, str]) -> bool:
        """Именно то содержимое, о котором запись, есть в каком-то коммите после записанного.

        Посторонний коммит (даже в тот же файл) запись не привязывает: нужен тот же blob.
        """
        if not blobs or not self.head or commit == self.head or not self.in_history(commit):
            return False
        return all(self._committed_blob(commit, p, b) for p, b in blobs.items())

    def history(self) -> set[str]:
        if self._history is None:
            out = self._git("rev-list", "--max-count=500000", "HEAD", timeout=60) if self.head else None
            self._history = set(out.split()) if out else set()
        return self._history

    def in_history(self, commit: str | None) -> bool:
        if not commit or not self.head:
            return False
        if commit in self.history():
            return True
        if len(commit) < 40:  # короткий хэш
            return any(c.startswith(commit) for c in self.history())
        return False

    def observed(self, paths=None) -> dict:
        """Штамп для новой записи или перемены. paths — файлы, о которых запись.

        «На незакоммиченных правках» запись считается, только если изменены именно ЕЁ файлы.
        Правки посторонних файлов на неё не влияют.
        """
        own = self.own_changes(paths) if self.head else []
        blobs = {p: self.blob_now(p) or "deleted" for p in own}
        return {"commit": self.head, "branch": self.branch, "scope": self.scope, "dirty": bool(own),
                "patch": json.dumps(blobs, sort_keys=True) if own else None}

    @staticmethod
    def _blobs_of(patch) -> dict[str, str]:
        """Отпечаток записи: путь -> blob. Отпечаток ранней сборки без содержимого — пустой
        (такая запись не считается действующей: консервативно)."""
        loaded = _load_patch(patch)
        return loaded if isinstance(loaded, dict) and all(isinstance(v, str) and (len(v) == 40 or v == "deleted")
                                                          for v in loaded.values()) else {}

    def classify(self, commit: str | None, scope: str | None, dirty, patch=None) -> str:
        if not commit:
            return "none"
        if dirty:
            blobs = self._blobs_of(patch)
            if scope != self.scope:
                # Чужие правки действуют здесь, только когда именно это содержимое уже пришло
                # в нашу историю коммитом после записанного (тот же blob по тому же пути) —
                # даже если исходная копия ещё не привязала запись (не писала в память после коммита).
                if self.in_history(commit) and self.changes_committed(commit, blobs):
                    return "history"
                return "pending"
            if not self.in_history(commit):
                return "other"
            if self.changes_present(blobs) or self.changes_committed(commit, blobs):
                return "here"
            return "withdrawn"
        if not self.head:
            return "none"
        return "history" if self.in_history(commit) else "other"

    def classify_record(self, rec: dict) -> str:
        return self.classify(rec.get("observed_commit"), rec.get("observed_scope"), rec.get("observed_dirty"),
                             rec.get("observed_patch"))

    def classify_transition(self, t: dict) -> str:
        return self.classify(t.get("at_commit"), t.get("scope"), t.get("dirty"), t.get("patch"))

    def effective_status(self, rec: dict, transitions: list[dict]) -> str:
        """Статус записи в этой рабочей копии: последняя перемена, которая здесь действует."""
        status = "active"
        for t in transitions:
            if self.classify_transition(t) in APPLIES:
                status = t["kind"]
        return status

    def can_anchor(self, commit: str | None, patch) -> bool:
        """Работу над файлами записи закоммитили — привязать запись к HEAD."""
        return bool(commit) and self.changes_committed(commit, self._blobs_of(patch))

    def describe(self, cls: str, rec: dict) -> str | None:
        """Человеческая пометка для записи, которая здесь не действует."""
        commit = (rec.get("observed_commit") or "")[:10]
        if cls == "other":
            return (f"recorded on another line of history ({rec.get('observed_branch') or '?'}@{commit}); "
                    "not established for this branch")
        if cls == "pending":
            return (f"recorded on uncommitted changes in worktree {rec.get('observed_scope')}; "
                    "not established until that work is committed")
        if cls == "withdrawn":
            return ("recorded on uncommitted changes that are no longer in this worktree "
                    "(reverted, stashed or another branch); not established")
        return None
