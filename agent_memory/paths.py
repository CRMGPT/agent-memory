"""Где лежит память и где корень кода.

Память общая для всех worktree одного репозитория и своя у каждого проекта. Корень кода —
текущий worktree: пути в доказательствах относительные и сверяются с тем кодом, над которым
агент работает сейчас.

Где хранилище:
  * `AGENT_MEMORY_DIR` — явно заданный каталог;
  * прежнее место `.agent-memory/` рядом с общим `.git` — если оно уже есть (память прежних
    версий); новые проекты туда не пишутся, чтобы не сорить в чужих рабочих копиях;
  * иначе — каталог пользователя `<данные>/agent-memory/projects/<id>/`, где id — устойчивый
    идентификатор проекта из файла `agent-memory.json` внутри общего каталога git (не в рабочей
    копии) или из явной метки `.agent-memory.json` в корне проекта без git. Отдельные клоны —
    разные `.git`, значит разные id и разные хранилища; worktree одного репозитория — один id.
Проект не определён (нет git и метки), корень — домашний каталог или корень диска, проект уже
принадлежит другой стороне (Windows или WSL), проект из WSL открыт нативным Windows — записи
нет, причина объясняется. Одно хранилище открывается только с одной стороны: SQLite в режиме
WAL нельзя открывать через сетевую файловую систему (\\wsl$, /mnt/c).

Общий `.git` не нашёлся — ошибка, а не тихий откат в каталог worktree.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .schema import MemoryError_

# Worktree, созданный из Windows, хранит в `.git` путь вида //wsl$/<дистрибутив>/<путь>;
# git внутри WSL такой путь не открывает, поэтому переводим его сами.
_WSL_UNC = re.compile(r"^(?://|\\\\)wsl(?:\$|\.localhost)[/\\][^/\\]+", re.IGNORECASE)


def _git(args: list[str], cwd: Path) -> str | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=10,
                             env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _local(path: str) -> str:
    if os.name == "posix":
        return _WSL_UNC.sub("", path).replace("\\", "/")
    return path


def _find_dotgit(cwd: Path) -> Path | None:
    for d in [cwd, *cwd.parents]:
        if (d / ".git").exists():
            return d / ".git"
    return None


def _common_from_dotgit(dotgit: Path) -> Path | None:
    if dotgit.is_dir():
        return dotgit
    text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(_local(text.split(":", 1)[1].strip()))
    if not gitdir.is_absolute():
        gitdir = (dotgit.parent / gitdir).resolve()
    commondir = gitdir / "commondir"
    if commondir.is_file():
        common = Path(_local(commondir.read_text(encoding="utf-8").strip()))
        common = common if common.is_absolute() else (gitdir / common).resolve()
    elif gitdir.parent.name == "worktrees":
        common = gitdir.parent.parent
    else:
        common = gitdir
    return common if common.is_dir() else None


def _gitdir_from_dotgit(dotgit: Path) -> Path | None:
    if dotgit.is_dir():
        return dotgit
    text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(_local(text.split(":", 1)[1].strip()))
    if not gitdir.is_absolute():
        gitdir = (dotgit.parent / gitdir).resolve()
    return gitdir if gitdir.is_dir() else None


def git_base(root: Path) -> list[str] | None:
    """Начало команды git для рабочей копии root или None, если это не git.

    Обычный путь — `git -C root`. Дерево, созданное из Windows, хранит в `.git` путь
    `//wsl$/...`, который git внутри WSL не открывает: тогда каталог git переводится
    вручную и передаётся явно.
    """
    root = Path(root)
    if _git(["rev-parse", "--git-dir"], root) is not None:
        return ["git", "-C", str(root)]
    dotgit = root / ".git"
    gitdir = _gitdir_from_dotgit(dotgit) if dotgit.exists() else None
    if gitdir is None:
        return None
    base = ["git", f"--git-dir={gitdir}", f"--work-tree={root}"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        ok = subprocess.run([*base, "rev-parse", "HEAD"], capture_output=True, timeout=10, env=env,
                            stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return base if ok else None


def default_scope(root: Path) -> str:
    """Имя рабочей копии плюс короткий хэш полного пути: два дерева с одним именем не путаются."""
    import hashlib

    tag = hashlib.sha1(str(Path(root).resolve()).encode()).hexdigest()[:6]
    return f"{Path(root).name}-{tag}"


def repo_root(cwd: Path | None = None) -> Path:
    env = os.environ.get("AGENT_MEMORY_REPO")
    if env:
        return Path(env).resolve()
    cwd = (cwd or Path.cwd()).resolve()
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    if top:
        return Path(top).resolve()
    dotgit = _find_dotgit(cwd)
    if dotgit:
        return dotgit.parent
    for d in [cwd, *cwd.parents]:
        if (d / ".agent-memory.json").is_file():
            return d
    return cwd


ID_FILE = "agent-memory.json"        # внутри общего каталога git
MARKER = ".agent-memory.json"        # явная метка корня проекта без git
DISABLE_MARKER = ".agent-memory-off"  # проект отключил автоматическое подключение


def side() -> str:
    """Какая сторона открывает хранилище: windows | wsl | linux | macos."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    rel = (Path("/proc/sys/kernel/osrelease").read_text(errors="replace") if Path("/proc/sys/kernel/osrelease")
           .exists() else "")
    return "wsl" if "microsoft" in rel.lower() or Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists() else "linux"


def data_home() -> Path:
    """Каталог данных пользователя для хранилищ проектов (не рабочие копии)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agent-memory"


def _is_unc_wsl(path: Path) -> bool:
    return sys.platform == "win32" and bool(_WSL_UNC.match(str(path)))


def _too_broad(root: Path) -> str | None:
    root = root.resolve()
    if root.parent == root:
        return f"{root} is a filesystem root, not a project"
    try:
        if root == Path.home().resolve():
            return f"{root} is the home directory, not a project"
    except RuntimeError:
        pass
    return None


@dataclass
class Project:
    root: Path | None
    kind: str                # git | marker | explicit | none
    store: Path | None
    project_id: str | None = None
    reason: str | None = None  # почему хранилища нет (запись запрещена)
    legacy: bool = False
    disabled: bool = False

    def as_dict(self) -> dict:
        return {"root": str(self.root) if self.root else None, "kind": self.kind,
                "store": str(self.store) if self.store else None, "project_id": self.project_id,
                "reason": self.reason, "legacy": self.legacy, "disabled": self.disabled, "side": side()}


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _norm_path(p: Path | str) -> str:
    s = str(Path(p).resolve()).replace("\\", "/")
    return s.lower() if sys.platform == "win32" else s


def _rewrite(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _claim(path: Path, create: bool) -> tuple[dict | None, str | None]:
    """Идентичность проекта из файла: (данные, причина отказа). Создаётся только писателем.

    В файле записан и путь к общему каталогу git. Путь другой, а исходный каталог по старому пути
    на месте и с тем же id — это КОПИЯ репозитория: у неё своя новая память (писатель заводит новый
    id, читатель видит пустую память). Исходного каталога больше нет — репозиторий ПЕРЕНЕСЛИ: id
    сохраняется, путь обновляет писатель."""
    data = _read_json(path) if path.exists() else None
    if path.exists() and (data is None or not isinstance(data.get("id"), str) or not data["id"]):
        return None, f"{path} is unreadable or has no id; fix or remove it"
    here = _norm_path(path.parent)
    if data is not None and data.get("common_dir") and data["common_dir"] != here \
            and (not data.get("side") or data["side"] == side()):
        original = _read_json(Path(data["common_dir"]) / path.name) if Path(data["common_dir"]).is_dir() else None
        if original and original.get("id") == data["id"]:  # копия: исходник на месте
            if not create:
                return None, None
            data = None
            path.unlink()
        elif create:  # перенос: тот же проект на новом месте
            data = {**data, "common_dir": here}
            _rewrite(path, data)
    elif data is not None and not data.get("common_dir") and create and data.get("side", side()) == side():
        data = {**data, "common_dir": here}
        _rewrite(path, data)
    if data is None:
        if not create:
            return None, None
        data = {"id": uuid.uuid4().hex, "side": side(), "common_dir": here, "created_by": "agent_memory"}
        tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        try:
            os.link(tmp, path)  # не перезаписывает файл, созданный параллельно
        except FileExistsError:
            pass
        except OSError:
            if not path.exists():
                os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        data = _read_json(path)
        if data is None:
            return None, f"{path} could not be created"
    if data.get("side") and data["side"] != side():
        return None, (f"this project's memory belongs to the {data['side']} side; it is opened only there "
                      f"(one store, one side: SQLite must not be shared across {data['side']} and {side()})")
    return data, None


def resolve_project(cwd: Path | None = None, create: bool = False) -> Project:
    """Проект текущего каталога и его хранилище. create=True — писатель может завести id."""
    cwd = (cwd or Path.cwd()).resolve()
    env = os.environ.get("AGENT_MEMORY_DIR")
    if env:
        return Project(repo_root(cwd), "explicit", Path(env).resolve())
    if _is_unc_wsl(cwd):
        return Project(None, "none", None, reason=f"{cwd} lives inside WSL: its memory is opened inside WSL, not "
                                                  "from Windows (the hook dispatches there)")
    common = None
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    git_common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    if git_common and Path(git_common).is_dir():
        common = Path(git_common).resolve()
    else:
        dotgit = _find_dotgit(cwd)
        found = _common_from_dotgit(dotgit) if dotgit else None
        if found:
            common, top = found.resolve(), top or str(dotgit.parent)
    if common is not None:
        root = Path(top).resolve() if top else common.parent
        broad = _too_broad(root)
        if broad:
            return Project(root, "git", None, reason=broad)
        disabled = (root / DISABLE_MARKER).exists() or (common.parent / DISABLE_MARKER).exists()
        legacy = common.parent / ".agent-memory"
        if legacy.is_dir():
            return Project(root, "git", legacy, legacy=True, disabled=disabled)
        data, why = _claim(common / ID_FILE, create)
        if why:
            return Project(root, "git", None, reason=why, disabled=disabled)
        if data is None:
            return Project(root, "git", None, disabled=disabled)  # памяти ещё нет: пусто
        return Project(root, "git", data_home() / "projects" / data["id"], data["id"], disabled=disabled)
    for d in [cwd, *cwd.parents]:
        if (d / MARKER).is_file():
            broad = _too_broad(d)
            if broad:
                return Project(d, "marker", None, reason=broad)
            data, why = _claim(d / MARKER, create=False)
            if why or data is None:
                return Project(d, "marker", None, reason=why or f"{d / MARKER} has no id")
            return Project(d, "marker", data_home() / "projects" / data["id"], data["id"],
                           disabled=(d / DISABLE_MARKER).exists())
        if _too_broad(d):
            break
    return Project(None, "none", None, reason=f"no git repository or {MARKER} marker above {cwd}: not a project "
                                             f"(run `python -m agent_memory init-project` in the project root, or set "
                                             "AGENT_MEMORY_DIR)")


def init_project(root: Path) -> dict:
    """Явно отметить корень проекта без git. Домашний каталог и корень диска — отказ."""
    root = root.resolve()
    broad = _too_broad(root)
    if broad:
        raise MemoryError_(broad)
    if _git(["rev-parse", "--show-toplevel"], root):
        raise MemoryError_(f"{root} is inside a git repository: its identity comes from git, no marker needed")
    data, why = _claim(root / MARKER, create=True)
    if why:
        raise MemoryError_(why)
    return {"root": str(root), "marker": str(root / MARKER), "project_id": data["id"]}


def store_dir(cwd: Path | None = None) -> Path:
    proj = resolve_project(cwd, create=True)
    if proj.store is None:
        raise MemoryError_(f"memory is not available here: {proj.reason or 'no project'}")
    return proj.store
