"""Хук Claude Code для автоматического подключения памяти (только чтение).

Запуск: `<python> bin/launcher.py hook <Событие>` (через hooks/run-hook.sh плагина), вход — JSON
события на stdin. События:
  SessionStart      — обзор памяти проекта (startup / resume / clear / compact); после clear и
                      compact счётчик уже показанного обнуляется: нужное снова доступно;
  UserPromptSubmit  — ограниченный набор записей по тексту запроса, без повтора уже
                      показанного в этой сессии; ранее показанная запись, которая здесь
                      больше не действует, объявляется одной строкой (WITHDRAWN) один раз.
Остальные события (Stop, SubagentStop, ...) намеренно ничего не делают: конец ответа — не
конец задачи; память пишет и завершает только исполнитель своей командой.

Правила: код выхода всегда 0 (никогда 2 — хук памяти ничего не блокирует); выдача — JSON
hookSpecificOutput.additionalContext, не больше OUTPUT_LIMIT знаков; входной JSON не
исполняется и не подставляется в команды; ошибка чтения — короткое сообщение без секретов.
Под Windows каталог проекта внутри WSL (\\\\wsl$\\<дистрибутив>\\...) передаётся тому же коду плагина
внутри WSL (путь к плагину переводится `wslpath`): хранилище открывается только с одной стороны.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

OUTPUT_LIMIT = 6000          # далеко ниже предела Claude Code в 10 000 знаков
SESSION_BUDGET = 3000
PROMPT_BUDGET = 4000
MAX_INPUT = 1_000_000
MAX_SHOWN = 400
EVENTS = ("SessionStart", "UserPromptSubmit")
# Claude Code останавливает хук через HOOK_TIMEOUT секунд (hooks/hooks.json). Все обращения к WSL
# укладываются в WSL_BUDGET от старта процесса хука, с запасом на запуск python и обёртки: холодный
# старт WSL даёт короткую подсказку, а не убитый хук.
HOOK_TIMEOUT = 15
WSL_BUDGET = 10.0
WSLPATH_MAX = 6.0       # перевод пути плагина (первый вызов может будить WSL)
MIN_DISPATCH = 2.0      # меньше этого на чтение памяти внутри WSL не запускаем
_clock = time.monotonic
_DEADLINE: float | None = None  # задаёт main(): старт процесса + WSL_BUDGET
_UNC = re.compile(r"^(?:\\\\|//)(?:wsl\$|wsl\.localhost)[\\/]([^\\/]+)(.*)$", re.IGNORECASE)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_LAST: dict = {}  # итог этого вызова для last_hook.json (doctor показывает последний вызов)


def _emit(event: str, text: str) -> None:
    if not text:
        return
    text = text[:OUTPUT_LIMIT]
    _LAST["chars"] = _LAST.get("chars", 0) + len(text)
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    data = json.dumps(out, ensure_ascii=False)
    try:
        sys.stdout.buffer.write(data.encode("utf-8"))
        sys.stdout.buffer.flush()
    except (AttributeError, OSError):
        print(data)


def _read_payload() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():  # запуск руками в терминале: события нет, не ждать ввода
            return {}
    except (AttributeError, ValueError, OSError):
        return {}
    raw = sys.stdin.buffer.read(MAX_INPUT + 1) if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode()
    if len(raw) > MAX_INPUT:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def wsl_target(cwd: str) -> tuple[str, str] | None:
    """\\\\wsl$\\<дистрибутив>\\home\\x -> ("<дистрибутив>", "/home/x"); иначе None."""
    m = _UNC.match(cwd or "")
    if not m:
        return None
    rest = m.group(2).replace("\\", "/") or "/"
    return m.group(1), rest if rest.startswith("/") else "/" + rest


def _cache_file(project_key: str, session_id: str) -> Path:
    from .plugin import data_dir

    sid = _SAFE.sub("_", session_id)[:80] or "nosession"
    return data_dir() / "sessions" / _SAFE.sub("_", project_key)[:80] / f"{sid}.json"


def _load_cache(path: Path) -> dict:
    """{"shown": {id: отпечаток}, "note": bool}. Отпечаток меняется вместе с фактом, его
    достоверностью или отзывом — тогда запись показывается снова."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"shown": {}, "note": False}
    if not isinstance(data, dict):
        return {"shown": {}, "note": False}
    shown = data.get("shown")
    if isinstance(shown, list):  # кэш прежней версии: только id
        shown = {rid: "" for rid in shown if isinstance(rid, str)}
    return {"shown": shown if isinstance(shown, dict) else {}, "note": bool(data.get("note"))}


def _save_cache(path: Path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        shown = dict(list(cache.get("shown", {}).items())[-MAX_SHOWN:])
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"shown": shown, "note": bool(cache.get("note")), "at": time.time()}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _state() -> dict:
    """Версия плагина и команды, которые запускают ЭТУ копию CLI на этой машине."""
    from . import plugin

    return {"version": plugin.version(), "commands": plugin.mem_commands()}


def integration_note(root: str, commands: list[tuple[str, str]], version: str | None) -> str:
    """ДОВЕРЕННАЯ служебная инструкция установленной интеграции (не данные памяти): как
    единственный исполнитель задачи ведёт цикл памяти этой командой. Выдаётся и при пустой памяти."""
    if not commands:
        return ""
    lines = "\n".join(f"- {name}: `{cmd}`" for name, cmd in commands)
    return (
        f'<memory-integration source="agent-memory plugin {version or "dev"}" trusted="true">\n'
        f"Automatic project memory is active for this project ({root}). This block comes from the installed "
        "agent-memory plugin, not from stored memory.\n"
        "For a task that changes or investigates code (not for plain conversation), the task's single executor "
        "(the main session when it works alone, otherwise the agent-memory:executor subagent) runs MEM from the "
        "project directory:\n"
        '1. Start: MEM begin "<short task>" - keep the printed session token for this task only (do not export '
        "it, do not pass it to other agents).\n"
        "2. After the result is checked: save only NEW verified knowledge - write a JSON file with a list of "
        'records, for example [{"type": "FACT", "section": "code", "statement": "...", "evidence": [{"kind": '
        '"file", "ref": "<path>"}]}] (type DECISION|FACT|CONSTRAINT|BUG, section code|logic|data|workflow), and '
        'run MEM end --session <token> --diff <file> --summary "<one line>"; nothing new -> MEM drop --session '
        '<token> --reason "no new knowledge".\n'
        "3. Only when the whole task is finished (review, fixes and delivery included): MEM session finish "
        "--session <token>. The end of a response is not the end of the task.\n"
        "Other agents (coordinator, agent-memory:researcher, agent-memory:reviewer) do not run MEM. A refusal (exit code not 0) is a real "
        "failure - report it; never add --operator-confirmed yourself. Never store secrets. If this project has its "
        "own memory instructions, follow them.\n"
        f"MEM on this machine:\n{lines}\n</memory-integration>")


def _commands_for(state: dict, linux_cwd: str | None = None) -> list[tuple[str, str]]:
    cmds = state.get("commands") or {}
    if linux_cwd is not None:
        from .plugin import fill_wsl_project  # путь проекта — в кавычках своей оболочки ($, `, ', ")

        out = []
        for key, label in (("wsl_from_git_bash", "Git Bash"), ("wsl_from_powershell", "PowerShell")):
            if cmds.get(key):
                filled = fill_wsl_project(cmds[key], key, linux_cwd)
                if filled is not None:
                    out.append((label, filled))
        return out
    labels = (("git_bash", "Git Bash"), ("powershell", "PowerShell"), ("shell", "shell"))
    return [(label, cmds[key]) for key, label in labels if cmds.get(key)]


def _dispatch_to_wsl(event: str, payload: dict, distro: str, linux_cwd: str, session_id: str) -> int:
    """Windows-хук для проекта внутри WSL: данные читает тот же код плагина внутри WSL (хранилище
    открывается только там; плагин виден из WSL по пути от `wslpath`), служебную инструкцию с
    командами для этой Windows-сессии добавляет Windows-сторона."""
    from . import plugin

    deadline = _DEADLINE if _DEADLINE is not None else _clock() + WSL_BUDGET
    slow = (f"[project memory] not loaded yet: WSL ({distro}) did not answer within {WSL_BUDGET:.0f} s "
            "(it may still be starting); memory loads with one of the next prompts.")
    t0 = _clock()
    wslpath_timeout = max(0.5, min(WSLPATH_MAX, deadline - t0))
    launcher = plugin.wsl_launcher(distro, timeout=wslpath_timeout)
    if not launcher:
        if _clock() - t0 >= wslpath_timeout - 0.05:
            _LAST["wsl_timeout"] = "wslpath"
            _emit(event, slow)
        else:
            _emit(event, f"[project memory] this project lives in WSL ({distro}); the plugin could not be reached "
                         "from inside WSL (wsl.exe or wslpath failed), so memory is not loaded here.")
        return 0
    remaining = deadline - _clock()
    if remaining < MIN_DISPATCH:
        _LAST["wsl_timeout"] = "budget"
        _emit(event, slow)
        return 0
    _LAST["dispatched"] = f"wsl:{distro}"
    payload = {**payload, "cwd": linux_cwd, "agent_memory_dispatched": True}
    # --exec: аргументы уходят в WSL как есть, без разбора оболочкой (пробелы, $, кавычки в путях);
    # байткод не пишется в каталог плагина на стороне Windows
    cmd = ["wsl.exe", "-d", distro, "--cd", "/", "--exec", "env", "PYTHONDONTWRITEBYTECODE=1", "python3", launcher,
           "hook", event]
    text = ""
    try:
        out = subprocess.run(cmd, input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=remaining)
        if out.returncode == 3:
            text = ("[project memory] not loaded: python3 inside WSL is older than 3.10 or missing; install "
                    "Python 3.10+ in the WSL distribution")
        else:
            parsed = json.loads(out.stdout[: OUTPUT_LIMIT * 4].decode("utf-8")) if out.stdout.strip() else {}
            text = str(parsed.get("hookSpecificOutput", {}).get("additionalContext", ""))
    except subprocess.TimeoutExpired:
        _LAST["wsl_timeout"] = "dispatch"
        text = slow
    except (OSError, ValueError, AttributeError) as exc:
        text = f"[project memory] not loaded: WSL did not answer ({type(exc).__name__})"
    note = ""
    if not text.startswith("[project memory] this project") and not text.startswith("[project memory] not loaded"):
        cache_path = _cache_file("wsl-" + hashlib.sha1(f"{distro}:{linux_cwd}".encode()).hexdigest()[:16], session_id)
        cache = _load_cache(cache_path)
        if event == "SessionStart" or not cache["note"]:
            state = {"version": plugin.version(), "commands": plugin.mem_commands(wsl_distro=distro,
                                                                                  wsl_launcher=launcher)}
            note = integration_note(f"{distro}:{linux_cwd}", _commands_for(state, linux_cwd), state.get("version"))
            cache["note"] = True
            _save_cache(cache_path, cache)
    _LAST["note_chars"] = len(note)
    _LAST["memory_chars"] = len(text) if not text.startswith("[project memory]") else 0
    _emit(event, "\n".join(x for x in (note, text) if x))
    return 0


def run(event: str, payload: dict) -> int:
    if event not in EVENTS:
        return 0
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return 0  # Claude Code всегда передаёт cwd; без него каталог не угадывается
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    _LAST["session"] = session_id
    if sys.platform == "win32":
        target = wsl_target(cwd)
        if target:
            return _dispatch_to_wsl(event, payload, *target, session_id)
    if not Path(cwd).is_dir():
        return 0
    from .paths import resolve_project
    from .reader import read_context

    proj = resolve_project(Path(cwd), create=False)
    if proj.disabled or proj.kind == "none":
        return 0
    dispatched = payload.get("agent_memory_dispatched") is True
    key_src = os.path.normcase(str(Path(cwd).resolve()))
    project_key = proj.project_id or hashlib.sha1(key_src.encode()).hexdigest()[:16]
    cache_path = _cache_file(project_key, session_id)
    if event == "SessionStart":
        source = payload.get("source")
        fresh = source in ("startup", "clear", "compact", None)
        cache = {"shown": {}, "note": False} if fresh else _load_cache(cache_path)
        # resume: прежний контекст на месте — перепроверить показанное и объявить отозванное
        res = read_context(cwd, None, SESSION_BUDGET, overview=True, recheck=None if fresh else dict(cache["shown"]))
    else:
        prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""
        if not prompt.strip():
            return 0
        cache = _load_cache(cache_path)
        res = read_context(cwd, prompt[:4000], PROMPT_BUDGET, exclude=cache["shown"])
    note = ""
    if not dispatched and (event == "SessionStart" or not cache["note"]):
        state = _state()
        note = integration_note(str(proj.root), _commands_for(state), state.get("version"))
        cache["note"] = bool(note) or cache["note"]
    data = ""
    if res["status"] == "unavailable":
        data = f"[project memory] not loaded: {res.get('reason')}"
    elif res["text"] and (event == "SessionStart" or res["shown"] or res.get("withdrawn")):
        data = res["text"]
        cache["shown"].update(res["shown"])
        for w in res.get("withdrawn") or []:  # объявлено один раз; снова подействует — покажется как новое
            cache["shown"].pop(w["id"], None)
    _save_cache(cache_path, cache)
    _LAST["note_chars"] = len(note)
    _LAST["memory_chars"] = len(data)
    _LAST["memory_status"] = res["status"]
    _emit(event, "\n".join(x for x in (note, data) if x))
    return 0


def _record_last(event: str, started: float, error: str | None) -> None:
    """<данные плагина>/last_hook.json: время и итог последнего вызова каждого события (без путей и текста)."""
    if not event:
        return
    from .plugin import data_dir

    path = data_dir() / "last_hook.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[event[:40]] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "elapsed_ms": round((time.monotonic() - started)
                        * 1000), "output_chars": _LAST.get("chars", 0), "note_chars": _LAST.get("note_chars", 0),
                        "memory_chars": _LAST.get("memory_chars", 0), "memory_status": _LAST.get("memory_status"),
                        "dispatched": _LAST.get("dispatched"), "wsl_timeout": _LAST.get("wsl_timeout"),
                        "error": error, "pid": os.getpid()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"last_hook.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    global _DEADLINE
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    started, error = time.monotonic(), None
    _DEADLINE = _clock() + WSL_BUDGET
    try:
        payload = _read_payload()
        if not event:
            event = payload.get("hook_event_name") if isinstance(payload.get("hook_event_name"), str) else ""
        return run(event, payload)
    except Exception as exc:  # noqa: BLE001 — хук памяти не ломает запуск Claude
        error = type(exc).__name__
        _emit(event or "SessionStart", f"[project memory] not loaded: {error}")
        return 0
    finally:
        _record_last(event, started, error)


if __name__ == "__main__":
    sys.exit(main())
