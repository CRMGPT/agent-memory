"""Аренда записи: из одной рабочей копии пишет одна сессия.

Сессия получает токен (`session start` или первый `begin`) и передаёт его в каждой
пишущей команде: `--session <токен>` или переменная AGENT_MEMORY_SESSION. Другой токен
в той же рабочей копии получает понятный отказ. Совместного редактирования одной ветки
двумя агентами нет и не будет.

Кто владелец. Агент Claude (и главная сессия, и любой её субагент) не имеет своего процесса
в системе: все они работают внутри одного процесса Claude Code (`claude.exe` настольного
приложения), который живёт, пока открыта сессия. Конец ответа субагента, его остановка или
завершение задачи процессом не отмечаются. Поэтому владельцем аренды считается процесс
Claude Code, из которого запущена команда памяти, а не фоновый «держатель»: прежний
`session hold` не видел ни конца задачи, ни сбоя агента.

Как он устанавливается. При `session start` (и первом `begin`, и `recover`) CLI сам, без
слов агента, проходит по цепочке своих родительских процессов до ближайшего процесса Claude
Code и записывает его pid, время создания и компьютер (`require`, то есть каждая запись,
систему не опрашивает). Способ зависит от ОС, правила владения одни:
  Windows (нативно) и WSL — powershell.exe и CIM (Win32_Process), процесс `claude.exe`;
      из WSL — через взаимодействие с Windows, если Claude Code запущен в Windows;
  Linux и macOS — `ps` (pid, ppid, время старта, имя), процесс `claude`, плюс boot id;
      время старта у `ps` — с точностью до секунды.

Состояние владельца (`session status`, `session recover`):
  dead    — записанный процесс Claude Code доказанно завершился: процесса с этим pid нет или
            под ним другой процесс (другое время создания). Восстановление разрешено.
  unknown — всё остальное, с кодом причины:
            host_alive_agent_unobservable — сессия Claude жива; работает ли ещё именно этот
                агент, снаружи не видно;
            no_interop / query_failed — нет связи с Windows или запрос не удался;
            other_host — запись сделана на другом компьютере;
            legacy_holder_record — аренда прежней версии с записью держателя;
            no_owner_captured — владелец не записан (CLI запущен не из Claude Code).
            Восстановление только `session recover --operator-confirmed "<причина>"`; причину
            даёт человек через координатора, агент её не подставляет.
Состояния alive нет: живой процесс Claude Code не доказывает, что агент ещё работает, а
живого владельца защищает то, что без доказанной смерти аренду отдаёт только человек.

Конец задачи — `session finish`: отказ при открытом эпизоде, снятие аренды, остановка
оставшихся держателей прежней версии с этим токеном (pid и время старта сверяются),
проверка, что своих процессов не осталось. Повторный вызов безопасен.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import faults
import sys

from .schema import LeaseError, ValidationError
from .store import SqliteStore, now

MAX_HISTORY = 20
HOST_PROCESS_NAME = "claude.exe"
POSIX_HOST_NAMES = ("claude",)


def _default_powershell() -> str:
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"  # WSL


# Пути к системным программам. Подменяются ТОЛЬКО в тестах внутри процесса (monkeypatch или
# обёртка запуска CLI в conftest); переменной окружения для этого нет намеренно.
WINDOWS_POWERSHELL: str | None = _default_powershell()
POSIX_PS: str | None = None if sys.platform == "win32" else "ps"
POWERSHELL_TIMEOUT = 40

UNKNOWN_TEXT = {
    "host_alive_agent_unobservable": "the Claude session is running; whether this particular agent is still "
                                     "working cannot be observed",
    "no_interop": "Windows cannot be queried from here (no WSL interop / powershell.exe)",
    "query_failed": "the Windows process query failed",
    "other_host": "the owner was recorded on another computer",
    "legacy_holder_record": "the lease was taken by an older version (background holder record only); the "
                            "holder's life says nothing about the agent",
    "no_owner_captured": "no Claude Code process was recorded as the owner of this lease",
    "incomplete_owner_record": "the recorded owner is incomplete (pid, creation time or computer name missing "
                               "or invalid), so it cannot be checked",
    "unsupported_platform": "this operating system has no supported way to check the owner process",
}


def _age(ts: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _json(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ================================================================ Windows: владелец


def _powershell(script: str) -> tuple[str, str]:
    """("ok", stdout) | ("no_interop", why) | ("query_failed", why)."""
    exe = WINDOWS_POWERSHELL
    if not exe or not Path(exe).exists():
        return "no_interop", f"{exe or 'powershell.exe'} is not available"
    try:
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                             stdin=subprocess.DEVNULL, timeout=POWERSHELL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "query_failed", f"{type(exc).__name__}: {exc}"
    if out.returncode != 0:
        return "query_failed", out.stderr.decode("utf-8", "replace").strip()[:300] or f"exit {out.returncode}"
    return "ok", out.stdout.decode("utf-8", "replace")


# Ответ PowerShell — в рамке: первая строка AMV1 BEGIN, последняя AMV1 END. Ответ без рамки,
# с лишними, пустыми или неполными полями — неполный, и из него не выводится ни «процесса нет»,
# ни «pid занят»: только unknown. Отсутствие процесса сообщается ЯВНО строкой PROC ABSENT, а не
# отсутствием строки. Любая ошибка CIM ($ErrorActionPreference = Stop) даёт ненулевой код выхода.
# Имя компьютера — из CIM (Win32_ComputerSystem), не из $env:COMPUTERNAME: переменную можно
# пробросить из WSL через WSLENV и так подменить «другой компьютер».
_BEGIN, _END = "AMV1 BEGIN", "AMV1 END"
_COMPUTER = '(Get-CimInstance Win32_ComputerSystem -Property Name).Name'
_FILETIME = '$(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { "" })'
_CAPTURE = ("$ErrorActionPreference = 'Stop'; " + f'"{_BEGIN}"; "SELF`t$PID`t$(' + _COMPUTER + ')"; '
            'Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name,CreationDate | '
            'ForEach-Object { "P`t{0}`t{1}`t{2}`t{3}" -f $_.ProcessId,$_.ParentProcessId,$_.Name,' + _FILETIME
            + ' }; ' + f'"{_END}"')


def _probe(pid: int) -> str:
    return ("$ErrorActionPreference = 'Stop'; " + f'"{_BEGIN}"; "HOST`t$(' + _COMPUTER + ')"; '
            f'$p = Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}" -Property ProcessId,Name,'
            'CreationDate; if ($p) { $p | ForEach-Object { "PROC`tPRESENT`t{0}`t{1}`t{2}" -f $_.ProcessId,'
            '$_.Name,' + _FILETIME + ' } } else { "PROC`tABSENT`t' + str(int(pid)) + '" }; ' + f'"{_END}"')


def _framed(text: str) -> list[list[str]] | None:
    """Строки ответа внутри рамки, разбитые по табуляции; None — рамки нет или она неполная."""
    lines = [ln.rstrip("\r") for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2 or lines[0] != _BEGIN or lines[-1] != _END:
        return None
    inner = lines[1:-1]
    if any(ln in (_BEGIN, _END) for ln in inner):
        return None
    return [ln.split("\t") for ln in inner]


def _positive_int(text: str) -> int | None:
    return int(text) if isinstance(text, str) and text.isdigit() and int(text) > 0 else None


def find_host(rows: dict[int, dict], start_pid: int, name: str = HOST_PROCESS_NAME) -> dict | None:
    """Ближайший предок start_pid с этим именем. Цепочка рвётся на родителе, созданном позже
    потомка (pid родителя достался новому процессу) — тогда владельца нет, без догадок."""
    cur = rows.get(start_pid)
    seen: set[int] = set()
    while cur and cur["pid"] not in seen and len(seen) < 64:
        seen.add(cur["pid"])
        parent = rows.get(cur["ppid"])
        if not parent or not parent["created"] or not cur["created"] or parent["created"] > cur["created"]:
            return None
        if parent["name"].lower() == name:
            return parent
        cur = parent
    return None


def _chain_break(rows: dict[int, dict], start_pid: int) -> str | None:
    """Имя процесса, у которого родитель уже завершился (цепочка оборвана), или None."""
    cur, seen = rows.get(start_pid), set()
    while cur and cur["pid"] not in seen and len(seen) < 64:
        seen.add(cur["pid"])
        parent = rows.get(cur["ppid"])
        if parent is None:
            return None if cur["ppid"] in (0, 4) else cur["name"]
        cur = parent
    return None


def _uncaptured(code: str, detail: str) -> dict:
    return {"kind": "uncaptured", "code": code, "detail": detail, "captured_at": now()}


def capture_owner() -> dict:
    """Процесс Claude Code, из которого запущена эта команда. Linux/macOS (и Claude Code,
    запущенный внутри WSL) — по своим процессам; Windows и WSL под Claude Code для Windows — по
    цепочке родителей Windows."""
    posix = _capture_posix() if POSIX_PS else None
    if posix and posix.get("kind") == "posix-host":
        return posix
    if sys.platform == "win32" or Path(WINDOWS_POWERSHELL or "").exists():
        return _capture_windows()
    return posix or _uncaptured("unsupported_platform", sys.platform)


def _capture_windows() -> dict:
    """Процесс Claude Code, из которого запущена эта команда: по цепочке родителей Windows.
    Неполный ответ или пустое имя компьютера не дают записи владельца, только причину."""
    status, text = _powershell(_CAPTURE)
    if status != "ok":
        return _uncaptured(status, text)
    rows_raw = _framed(text)
    if rows_raw is None:
        return _uncaptured("query_failed", "incomplete or malformed answer (no AMV1 frame)")
    selfs = [r for r in rows_raw if r[0] == "SELF"]
    if len(selfs) != 1 or len(selfs[0]) != 3 or _positive_int(selfs[0][1]) is None:
        return _uncaptured("query_failed", "incomplete or malformed answer (SELF line)")
    me, computer = int(selfs[0][1]), selfs[0][2].strip()
    if not computer:
        return _uncaptured("incomplete_owner_record", "empty computer name in the answer")
    rows: dict[int, dict] = {}
    for r in rows_raw:
        if r[0] == "SELF":
            continue
        if r[0] != "P" or len(r) != 5 or not r[1].isdigit() or not r[2].isdigit() or not r[3] \
                or (r[4] and not r[4].isdigit()):
            return _uncaptured("query_failed", "incomplete or malformed answer (process line)")
        rows[int(r[1])] = {"pid": int(r[1]), "ppid": int(r[2]), "name": r[3], "created": int(r[4] or 0)}
    if not rows:
        return _uncaptured("query_failed", "incomplete or malformed answer (no process lines)")
    host = find_host(rows, me)
    if not host:
        broken = _chain_break(rows, me)
        why = (f"the Windows parent chain breaks at {broken} (its parent process has already exited - typical "
               "when the command is started through an MSYS/Git Bash shim such as `env` or `exec`); start the "
               "command directly from the shell") if broken else \
            f"no {HOST_PROCESS_NAME} among the Windows ancestors of this command (not started from Claude Code)"
        return _uncaptured("no_owner_captured", why)
    return {"kind": "claude-host", "name": host["name"], "pid": host["pid"], "created": str(host["created"]),
            "computer": computer, "wsl_host": socket.gethostname(), "captured_at": now(),
            "via": "powershell.exe parent chain" + ("" if sys.platform == "win32" else " through WSL interop")}


def owner_record_complete(rec: dict) -> bool:
    """Запись владельца пригодна для проверки: pid, время создания и компьютер заданы и корректны."""
    if rec.get("kind") == "posix-host" and not (isinstance(rec.get("boot"), str) and rec["boot"].strip()):
        return False
    return (rec.get("kind") in ("claude-host", "posix-host") and _positive_int(str(rec.get("pid", ""))) is not None
            and _positive_int(str(rec.get("created", ""))) is not None
            and isinstance(rec.get("computer"), str) and bool(rec["computer"].strip()))


def host_status(rec: dict) -> dict:
    """Жив ли записанный процесс Claude Code: alive | gone | reused | unknown (+code).

    gone/reused — только по полному ответу в рамке с тем же непустым именем компьютера и явной
    строкой PROC про ЭТОТ pid. Всё прочее — unknown."""
    base = {"name": rec.get("name"), "pid": rec.get("pid"), "computer": rec.get("computer")}
    if not owner_record_complete(rec):
        return {**base, "state": "unknown", "code": "incomplete_owner_record"}
    if rec["kind"] == "posix-host":
        return _posix_status(rec, base)
    pid, created = int(rec["pid"]), int(rec["created"])
    status, text = _powershell(_probe(pid))
    if status != "ok":
        return {**base, "state": "unknown", "code": status, "detail": text}

    def incomplete(why: str) -> dict:
        return {**base, "state": "unknown", "code": "query_failed", "detail": f"incomplete or malformed answer "
                                                                              f"({why})"}

    rows = _framed(text)
    if rows is None:
        return incomplete("no AMV1 frame")
    hosts = [r for r in rows if r[0] == "HOST"]
    procs = [r for r in rows if r[0] == "PROC"]
    if len(hosts) + len(procs) != len(rows) or len(hosts) != 1 or len(procs) != 1:
        return incomplete("expected exactly one HOST and one PROC line")
    if len(hosts[0]) != 2 or not hosts[0][1].strip():
        return incomplete("empty computer name")
    computer = hosts[0][1].strip()
    if computer != rec["computer"].strip():
        return {**base, "state": "unknown", "code": "other_host", "detail": f"checked from {computer!r}"}
    proc = procs[0]
    if len(proc) == 3 and proc[1] == "ABSENT" and proc[2] == str(pid):
        return {**base, "state": "gone"}
    if len(proc) == 5 and proc[1] == "PRESENT" and proc[2] == str(pid) and proc[3].strip():
        now_created = _positive_int(proc[4])
        if now_created is None:
            return incomplete("process line without a valid creation time")
        if now_created != created:
            return {**base, "state": "reused", "now": {"name": proc[3], "created": now_created}}
        return {**base, "state": "alive"}
    return incomplete("process line")


# ===================================================== Linux / macOS: владелец через ps


def _boot_id() -> str | None:
    text = _read("/proc/sys/kernel/random/boot_id")
    if text and text.strip():
        return text.strip()
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=10,
                                 stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        sec = out.stdout.split("sec =", 1)[1].split(",", 1)[0].strip() if "sec =" in out.stdout else ""
        return f"darwin-boot-{sec}" if out.returncode == 0 and sec.isdigit() else None
    return None


def _ps_table() -> dict[int, dict] | None:
    """Все процессы: pid -> ppid, время старта (секунды эпохи), имя. None — ответ неполный:
    ошибка ps, нечитаемая строка или в таблице нет самого этого процесса."""
    try:
        out = subprocess.run([POSIX_PS, "-A", "-o", "pid=", "-o", "ppid=", "-o", "lstart=", "-o", "comm="],
                             capture_output=True, text=True, timeout=20,
                             env={**os.environ, "LC_ALL": "C", "LANG": "C"}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired, TypeError):
        return None
    if out.returncode != 0:
        return None
    rows: dict[int, dict] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 8 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        try:
            started = int(time.mktime(time.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y")))
        except ValueError:
            return None
        rows[int(parts[0])] = {"pid": int(parts[0]), "ppid": int(parts[1]), "created": started,
                               "name": os.path.basename(" ".join(parts[7:]))}
    return rows if os.getpid() in rows else None


def _capture_posix() -> dict:
    rows = _ps_table()
    if rows is None:
        return _uncaptured("query_failed", "ps answer incomplete or unreadable")
    cur, seen = rows.get(os.getpid()), set()
    while cur and cur["pid"] not in seen and len(seen) < 64:
        seen.add(cur["pid"])
        parent = rows.get(cur["ppid"])
        if not parent or parent["created"] > cur["created"]:
            break
        if parent["name"] in POSIX_HOST_NAMES:
            boot = _boot_id()
            if not boot:
                return _uncaptured("incomplete_owner_record", "boot id unavailable")
            return {"kind": "posix-host", "name": parent["name"], "pid": parent["pid"],
                    "created": str(parent["created"]), "computer": socket.gethostname(), "boot": boot,
                    "platform": sys.platform, "captured_at": now(), "via": "ps parent chain"}
        cur = parent
    return _uncaptured("no_owner_captured", f"no {'/'.join(POSIX_HOST_NAMES)} process among the ancestors of "
                                            "this command")


def _posix_status(rec: dict, base: dict) -> dict:
    """gone/reused — только по полной таблице ps на том же компьютере; другой boot id — машина
    перезагружалась, значит записанного процесса больше нет."""
    if not POSIX_PS:
        return {**base, "state": "unknown", "code": "unsupported_platform"}
    computer = socket.gethostname()
    if not computer or computer != rec["computer"]:
        return {**base, "state": "unknown", "code": "other_host", "detail": f"checked from {computer!r}"}
    boot = _boot_id()
    if not boot:
        return {**base, "state": "unknown", "code": "query_failed", "detail": "boot id unavailable"}
    if boot != rec["boot"]:
        return {**base, "state": "gone", "detail": "the machine rebooted since the owner was recorded"}
    rows = _ps_table()
    if rows is None:
        return {**base, "state": "unknown", "code": "query_failed", "detail": "ps answer incomplete"}
    proc = rows.get(int(rec["pid"]))
    if proc is None:
        return {**base, "state": "gone"}
    if proc["created"] != int(rec["created"]):
        return {**base, "state": "reused", "now": {"name": proc["name"], "created": proc["created"]}}
    return {**base, "state": "alive"}


def owner_state(owner_rec: dict | None, holder_rec: dict | None = None) -> dict:
    """Состояние владельца аренды: dead только по доказательству, иначе unknown с кодом."""
    def unknown(code: str, host: dict | None = None, detail: str | None = None) -> dict:
        out = {"state": "unknown", "code": code, "reason": UNKNOWN_TEXT[code]}
        if detail:
            out["reason"] += f" ({detail})"
        if host is not None:
            out["host"] = host
        return out

    if not owner_rec:
        return unknown("legacy_holder_record" if holder_rec else "no_owner_captured")
    if owner_rec.get("kind") not in ("claude-host", "posix-host"):
        code = owner_rec.get("code") if owner_rec.get("code") in UNKNOWN_TEXT else "no_owner_captured"
        return unknown(code, detail=f"at capture: {owner_rec.get('detail')}" if owner_rec.get("detail") else None)
    host = host_status(owner_rec)
    if host["state"] == "gone":
        return {"state": "dead", "code": "host_gone", "host": host,
                "reason": f"Claude Code process {owner_rec['pid']} on {owner_rec.get('computer')} has exited"}
    if host["state"] == "reused":
        return {"state": "dead", "code": "host_reused", "host": host,
                "reason": f"pid {owner_rec['pid']} now belongs to another process (creation time differs): the "
                          "recorded Claude Code process has exited"}
    if host["state"] == "alive":
        return unknown("host_alive_agent_unobservable", host)
    return unknown(host.get("code", "query_failed"), host, host.get("detail"))


# ======================================================= прежний держатель (WSL)


def proc_identity(pid: int) -> dict | None:
    """Кто сейчас под этим pid в этой ОС: pid, ppid, время старта. None — процесса нет или зомби."""
    text = _read(f"/proc/{int(pid)}/stat")
    if not text or ")" not in text:
        return None
    fields = text.rsplit(")", 1)[1].split()  # имя процесса может содержать пробелы и скобки
    if len(fields) < 20 or fields[0] in ("Z", "X"):
        return None
    return {"host": socket.gethostname(), "pid": int(pid), "ppid": int(fields[1]), "start": fields[19],
            "boot_id": (_read("/proc/sys/kernel/random/boot_id") or "").strip() or None}


def _holder_processes(token: str) -> list[dict]:
    """Процессы `agent_memory session hold` этого токена (в командной строке или окружении)."""
    found = []
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else []:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [a.decode("utf-8", "replace") for a in argv if a]
        if "agent_memory" not in " ".join(words) or "hold" not in words:
            continue
        mine = token in words
        if not mine:
            try:
                mine = f"AGENT_MEMORY_SESSION={token}".encode() in (entry / "environ").read_bytes().split(b"\0")
            except OSError:
                mine = False
        ident = proc_identity(int(entry.name))
        if mine and ident:
            found.append({"pid": ident["pid"], "start": ident["start"], "cmd": " ".join(words)[:200]})
    return found


def _stop_holders(token: str, registered: dict | None) -> list[dict]:
    """Остановить держатели прежней версии с этим токеном. Сигнал — только процессу, у которого
    pid И время старта совпадают с найденными (повторно используемый pid не трогаем)."""
    targets = {p["pid"]: p for p in _holder_processes(token)}
    if registered and registered.get("pid") and registered.get("host") == socket.gethostname():
        cur = proc_identity(int(registered["pid"]))
        cmd = _read(f"/proc/{int(registered['pid'])}/cmdline") or ""
        if cur and str(cur["start"]) == str(registered.get("start")) and "agent_memory" in cmd:
            targets.setdefault(cur["pid"], {"pid": cur["pid"], "start": cur["start"], "cmd": "registered holder"})
    stopped = []
    for p in targets.values():
        for sig, wait in ((signal.SIGTERM, 3.0), (getattr(signal, "SIGKILL", signal.SIGTERM), 3.0)):
            cur = proc_identity(p["pid"])
            if not cur or str(cur["start"]) != str(p["start"]):
                break
            try:
                os.kill(p["pid"], sig)
            except OSError:
                break
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                cur = proc_identity(p["pid"])
                if not cur or str(cur["start"]) != str(p["start"]):
                    break
                time.sleep(0.05)
        stopped.append({"pid": p["pid"], "cmd": p["cmd"]})
    return stopped


# ================================================================== аренда


class Leases:
    def __init__(self, store: SqliteStore, scope: str):
        self.store = store
        self.scope = scope

    def status(self, check_owner: bool = False) -> dict | None:
        """check_owner=True спрашивает Windows (около секунды): только для status/start/recover,
        никогда внутри транзакции записи и не в `require`."""
        row = self.store.k.conn.execute("select * from leases where scope=?", (self.scope,)).fetchone()
        if not row:
            return None
        lease = dict(row)
        lease["idle_s"] = round(_age(lease["heartbeat_at"]))
        lease["holder_proc"] = _json(lease.get("holder_proc"), None)
        lease["owner_proc"] = _json(lease.get("owner_proc"), None)
        lease["history"] = _json(lease.get("history"), [])
        lease["owner"] = (owner_state(lease["owner_proc"], lease["holder_proc"]) if check_owner
                          else {"state": "not_checked", "reason": "owner state is checked by `session status`"})
        return lease

    def _describe(self, lease: dict) -> str:
        owner = lease["owner"]
        text = (f"worktree {self.scope} is held by session {lease['token']} ({lease.get('holder')}), "
                f"last activity {lease['idle_s']} s ago")
        if owner["state"] != "not_checked":
            text += f", owner {owner['state']} ({owner.get('code')}): {owner['reason']}"
        return text

    def start(self, label: str | None = None) -> str:
        from .memory import refuse_secrets

        refuse_secrets(label, what="the session label")
        owner = capture_owner()  # до транзакции записи: около двух секунд
        with self.store.transaction():
            if not self.status():
                token = "s-" + secrets.token_hex(8)
                holder = f"{label or 'agent'}@{socket.gethostname()}:{os.getpid()}"
                self.store.k.conn.execute(
                    "insert into leases(scope, token, holder, acquired_at, heartbeat_at, ttl_s, owner_proc)"
                    " values(?,?,?,?,?,?,?)",
                    (self.scope, token, holder, now(), now(), 0, json.dumps(owner, ensure_ascii=False)),
                )
                return token
        lease = self.status(check_owner=True)
        if not lease:
            raise LeaseError(f"the lease on worktree {self.scope} changed while starting; run it again")
        if lease["owner"]["state"] == "dead":
            raise LeaseError(self._describe(lease) + ". That session is proven gone: take over with "
                             "`session recover`.")
        raise LeaseError(self._describe(lease) + ". One writer per worktree: use another worktree and branch, "
                         "or wait for that session to finish the task (`session finish`).")

    def require(self, token: str | None) -> None:
        """Пишущая команда: токен должен держать аренду этой рабочей копии. Время простоя не важно,
        Windows не опрашивается."""
        lease = self.status()
        if not token:
            if lease:
                raise LeaseError(self._describe(lease) + ". Pass your token with --session or "
                                 "AGENT_MEMORY_SESSION; a second writer in one worktree is refused.")
            raise LeaseError(f"no session for worktree {self.scope}: run `session start` (or `begin`) "
                             "and pass the token with --session / AGENT_MEMORY_SESSION")
        if not lease or lease["token"] != token:
            if lease:
                raise LeaseError(self._describe(lease) + f"; your token {token} does not own it")
            raise LeaseError(f"session {token} holds no lease on worktree {self.scope}; run `session start`")
        self.store.k.conn.execute("update leases set heartbeat_at=? where scope=? and token=?",
                                  (now(), self.scope, token))

    def recover(self, label: str | None = None, operator_confirmed: str | None = None) -> dict:
        """Забрать аренду, владелец которой доказанно завершился, или — при неизвестном состоянии —
        только с подтверждением человека. Открытый эпизод переходит новой сессии с отметкой."""
        from .memory import refuse_secrets

        refuse_secrets(label, operator_confirmed, what="the recovery label or reason")
        reason = (operator_confirmed or "").strip() or None
        if operator_confirmed is not None and not reason:
            raise LeaseError("--operator-confirmed needs a reason from the human: what confirmed that the "
                             "owner's task is over")
        lease = self.status(check_owner=True)  # опрос Windows вне транзакции записи
        state = lease["owner"] if lease else {"state": "none", "reason": "no lease on this worktree"}
        if state["state"] == "unknown" and not reason:
            raise LeaseError(self._describe(lease) + ". Owner state is unknown: recovery refused, silence or a "
                             "running session is not proof that the owner's task ended. Only the human may "
                             "confirm it; the coordinator then passes that confirmation to the executor, who runs "
                             "`session recover --operator-confirmed \"<what the human confirmed>\"`. An agent "
                             "never supplies this flag on its own.")
        owner = capture_owner()
        with self.store.transaction(commit_fault="recover_commit"):
            cur = self.status()
            if (cur and cur["token"]) != (lease and lease["token"]):
                raise LeaseError(f"the lease on worktree {self.scope} changed while checking the owner; "
                                 "run `session recover` again")
            token = "s-" + secrets.token_hex(8)
            holder = f"{label or 'agent'}@{socket.gethostname()}:{os.getpid()}"
            old = lease["token"] if lease else None
            entry = {"at": now(), "from": old, "to": token, "owner_state": state["state"],
                     "code": state.get("code"), "reason": state["reason"], "operator_confirmed": reason}
            history = ((lease or {}).get("history") or [])[-(MAX_HISTORY - 1):] + [entry]
            self.store.k.conn.execute(
                "insert into leases(scope, token, holder, acquired_at, heartbeat_at, ttl_s, previous_token,"
                " holder_proc, owner_proc, history) values(?,?,?,?,?,?,?,NULL,?,?) on conflict(scope) do update"
                " set token=excluded.token, holder=excluded.holder, acquired_at=excluded.acquired_at,"
                " heartbeat_at=excluded.heartbeat_at, ttl_s=excluded.ttl_s,"
                " previous_token=excluded.previous_token, holder_proc=NULL, owner_proc=excluded.owner_proc,"
                " history=excluded.history",
                (self.scope, token, holder, now(), now(), 0, old, json.dumps(owner, ensure_ascii=False),
                 json.dumps(history, ensure_ascii=False)),
            )
            adopted = None
            ep = self.store.open_episode(self.scope)
            if ep:
                self.store.update_episode(ep["id"], owner_token=token)
                text = (f"session {token} took over from session {old or ep.get('owner_token')}: "
                        f"owner {state['state']} ({state['reason']})")
                if reason:
                    text += f"; operator confirmed: {reason}"
                self.store.add_note(ep["id"], "recovery", text, None)
                adopted = ep["id"]
        return {"token": token, "previous_token": old, "adopted_episode": adopted, "owner_state": state,
                "operator_confirmed": reason, "owner": owner}

    def release(self, token: str) -> None:
        """Низкоуровневое снятие аренды. Конец задачи — `finish` (проверяет эпизод и процессы)."""
        with self.store.transaction():
            lease = self.status()
            if not lease or lease["token"] != token:
                raise LeaseError(f"session {token} does not hold worktree {self.scope}")
            self.store.k.conn.execute("delete from leases where scope=? and token=?", (self.scope, token))

    def _finished_key(self) -> str:
        return f"lease_finished:{self.scope}"

    def _finished_tokens(self) -> list[str]:
        return _json(self.store.k.meta(self._finished_key()), [])

    def finish(self, token: str | None) -> dict:
        """Конец ВСЕЙ задачи (не конца ответа): снять свою аренду, остановить свои держатели прежней
        версии, доказать, что своих процессов не осталось. Повторный вызов тем же токеном после
        успешного или оборванного finish безопасен; чужой токен получает отказ (код 4)."""
        if not token:
            raise LeaseError("session finish needs your token: --session or AGENT_MEMORY_SESSION")
        with self.store.transaction():
            lease = self.status()
            mine = bool(lease and lease["token"] == token)
            finished_before = token in self._finished_tokens()
            ep = self.store.open_episode(self.scope)
            if not mine and not finished_before:
                where = (f"worktree {self.scope} is held by session {lease['token']}" if lease
                         else f"worktree {self.scope} has no lease")
                raise LeaseError(f"{where}; session {token} does not own it and has not finished here, so "
                                 "there is nothing to finish for it (nothing changed)")
            if ep and (mine or ep.get("owner_token") == token):
                raise LeaseError(f"episode {ep['id']} is still open: run `end` (or `drop`) first. `session "
                                 "finish` is for the end of the whole task, after review, fixes and delivery.")
            registered = lease["holder_proc"] if mine else None
            if mine:
                self.store.k.conn.execute("delete from leases where scope=? and token=?", (self.scope, token))
                done = [t for t in self._finished_tokens() if t != token][-(MAX_HISTORY - 1):] + [token]
                self.store.k.set_meta(self._finished_key(), json.dumps(done))
            other = lease["token"] if lease and not mine else None
            open_ep = {"id": ep["id"], "owner_token": ep.get("owner_token")} if ep else None
        faults.hit("finish_after_release")  # аренда снята, держатели ещё не остановлены
        stopped = _stop_holders(token, registered)
        residual = _holder_processes(token)
        return {"finished": token, "worktree": self.scope, "lease_released_now": mine,
                "already_finished_before": finished_before and not mine,
                "lease_now": {"token": other} if other else None, "open_episode": open_ep,
                "legacy_holders_stopped": stopped, "own_residual_processes": residual,
                "new_start_possible": other is None and open_ep is None and not residual}

    def hold(self, token: str | None = None) -> None:
        raise ValidationError(
            "`session hold` is retired: a background holder cannot see whether the Claude agent that started it "
            "is still working, so its death proved nothing and its life kept finished tasks locked. The owner is "
            "now the Claude Code process recorded at `session start`/`begin`; nothing has to run in the "
            "background. At the end of the whole task run `session finish`.")
