"""CLI памяти: `python3 -m agent_memory <команда>`. Справка: `--help` у каждой команды.

Коды выхода:
  0 — успех;
  2 — ошибка ввода, НИЧЕГО не сохранено;
  3 — конфликт (запись изменилась после чтения, спор по subject), ничего не сохранено;
  4 — аренда: рабочую копию держит другая сессия или не передан токен, ничего не сохранено;
  5 — хранилище старой схемы: нужен шаг владельца `migrate`;
  6 — база СОХРАНЕНА, но производный файл (архив, working.md) не записан: `repair`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import maintenance, metrics, migrate
from . import schema as S
from .context import Context
from .memory import Memory
from .paths import init_project, repo_root, resolve_project, store_dir

DEFAULT_ROOTS = ("app", "scripts", "tests", "agent_memory", "src", "lib")


def _roots(root: Path) -> tuple[str, ...]:
    env = os.environ.get("AGENT_MEMORY_ROOTS")
    if env:
        return tuple(r.strip() for r in env.split(",") if r.strip())
    found = tuple(r for r in DEFAULT_ROOTS if (root / r).is_dir())
    return found or (".",)


def _load(arg: str | None) -> dict | None:
    if arg is None:
        return None
    if arg == "-":
        return json.load(sys.stdin)
    if arg.lstrip().startswith(("{", "[")):
        return json.loads(arg)
    return json.loads(Path(arg).read_text(encoding="utf-8"))


SECTION_HELP = ("comma-separated sections: code, logic, data, workflow (and unclassified for records from "
                "before sections); default: all")


def _sections(arg: str | None) -> tuple[str, ...] | None:
    if not arg:
        return None
    names = tuple(x.strip() for x in arg.split(",") if x.strip())
    bad = [x for x in names if x not in (*S.SECTIONS, S.UNCLASSIFIED)]
    if bad:
        raise S.ValidationError(f"unknown section(s) {bad}; use {S.SECTIONS} or {S.UNCLASSIFIED}")
    return names


def _out(data, as_json: bool = True) -> None:
    if isinstance(data, str) and not as_json:
        print(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=1, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent_memory", description="Persistent memory for a coding agent")
    p.add_argument("--store", help="memory dir (default: <main checkout>/.agent-memory or $AGENT_MEMORY_DIR)")
    p.add_argument("--repo", help="code root (default: current git worktree or $AGENT_MEMORY_REPO)")
    p.add_argument("--session", help="session token for writes (or $AGENT_MEMORY_SESSION)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create store and show where it lives")
    sub.add_parser("where", help="read-only: which project and store this directory maps to (creates nothing)")
    sub.add_parser("init-project", help="mark the current directory as a project root (only without git)")
    s = sub.add_parser("session", help="write lease of this worktree: start | status | recover | adopt | finish "
                                       "| release (hold is retired)")
    s.add_argument("action", choices=("start", "hold", "status", "recover", "adopt", "finish", "release"))
    s.add_argument("--label")
    s.add_argument("--operator-confirmed", metavar="REASON",
                   help="recover only, owner state unknown: what the HUMAN confirmed (passed on by the "
                        "coordinator); recorded. An agent never supplies it on its own")
    s = sub.add_parser("index", help="incremental AST index of THIS worktree's code")
    s.add_argument("paths", nargs="*")
    s.add_argument("--progress", action="store_true")
    s.add_argument("--full", action="store_true", help="re-read every file, ignoring the size/mtime/hash cache")

    for name, hlp in (("begin", "start an episode: task frame -> retrieval -> memory pack"),
                      ("build", "memory pack for a task without opening an episode (logs a metric: executor "
                                "only; hooks use the read-only reader)")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("task")
        s.add_argument("--section", help=SECTION_HELP)
        s.add_argument("--frame", help="task frame JSON (inline, file or -)")
        s.add_argument("--budget", type=int)
        s.add_argument("--json", action="store_true")
    s = sub.add_parser("expand", help="lazy lookup inside an episode (memory page fault)")
    s.add_argument("query")
    s.add_argument("--section", help=SECTION_HELP)
    s.add_argument("--budget", type=int, default=2000)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("note", help="temporary episode note; its ref note:<episode>#<seq> can back tool evidence")
    s.add_argument("text")
    s.add_argument("--kind", default="note")
    s.add_argument("--ref")
    s = sub.add_parser("end", help="finish episode: knowledge diff + close + archive in one transaction")
    s.add_argument("--diff", help="knowledge diff JSON (inline, file or -)")
    s.add_argument("--summary", help="one-line episode summary (stored as EPISODE record)")
    s.add_argument("--working", help="new working.md content file")
    s = sub.add_parser("drop", help="abandon episode: archive notes, commit nothing")
    s.add_argument("--reason", default="")
    sub.add_parser("status", help="lease, open episode, code index freshness, pending files")
    sub.add_parser("repair", help="rewrite derived files (episode archives, working.md) from the database")

    s = sub.add_parser("commit", help="apply a knowledge diff outside an episode")
    s.add_argument("diff")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--section", help=SECTION_HELP)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--history", action="store_true", help="include superseded/invalidated")
    for name in ("get", "sources", "why"):
        s = sub.add_parser(name)
        s.add_argument("id")
    s = sub.add_parser("related")
    s.add_argument("id")
    s.add_argument("--depth", type=int, default=1)
    s = sub.add_parser("history", help="all versions of claims about one subject, as seen here")
    s.add_argument("subject")
    s = sub.add_parser("add")
    s.add_argument("record", help="record JSON (inline, file or -)")
    s = sub.add_parser("update")
    s.add_argument("id")
    s.add_argument("--patch", required=True)
    s.add_argument("--expected-version", type=int, required=True)
    s.add_argument("--evidence")
    s.add_argument("--reason")
    s = sub.add_parser("invalidate")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.add_argument("--expected-version", type=int, required=True)
    s.add_argument("--evidence")
    s = sub.add_parser("link")
    s.add_argument("src")
    s.add_argument("rel")
    s.add_argument("dst")
    s.add_argument("--origin", default="asserted", choices=("asserted", "inferred"))

    s = sub.add_parser("working", help="show or replace working.md (stored in the database)")
    s.add_argument("action", choices=("show", "set"))
    s.add_argument("file", nargs="?")
    sub.add_parser("compact", help="drop working.md lines that cite records no longer active here")
    s = sub.add_parser("maintain", help="report duplicates, contradictions, stale, orphans")
    s.add_argument("--apply-safe", action="store_true")
    sub.add_parser("stats", help="storage and retrieval metrics (approximate sizes)")
    s = sub.add_parser("migrate", help="OWNER STEP: upgrade the store schema (verified backup first)")
    s.add_argument("--restore", help="restore this verified backup file instead of migrating")
    # Токен принимается и после команды (`note "x" --session s-...`). SUPPRESS: подкоманда без
    # --session не затирает значение, переданное до неё.
    for cmd in sub.choices.values():
        cmd.add_argument("--session", dest="session", default=argparse.SUPPRESS,
                         help="session token for writes (or $AGENT_MEMORY_SESSION)")
    return p


def _utf8_pipes() -> None:
    """Вывод в канал (агент, хук, тест) — всегда UTF-8, на любой ОС; консоль не трогаем."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty() and (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_pipes()
    args = build_parser().parse_args(argv)
    if args.cmd in ("where", "init-project"):
        try:
            _out({**resolve_project().as_dict(), "code_path": str(Path(__file__).resolve().parent)}
                 if args.cmd == "where" else init_project(Path.cwd()))
            return 0
        except S.MemoryError_ as exc:
            print(f"ERROR (nothing saved): {exc}", file=sys.stderr)
            return 2
    root = Path(args.repo).resolve() if args.repo else repo_root()
    try:
        sdir = Path(args.store).resolve() if args.store else store_dir()
    except S.MemoryError_ as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "migrate":
        try:
            _out(migrate.restore(sdir, Path(args.restore)) if args.restore else migrate.migrate(sdir))
            return 0
        except S.MemoryError_ as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    try:
        mem = Memory(sdir, root, roots=_roots(root))
    except S.MigrationRequired as exc:
        print(f"MIGRATION REQUIRED: {exc}", file=sys.stderr)
        return 5
    ctx = Context(mem, session=args.session)
    try:
        # Производные файлы из базы после любого прошлого сбоя. Сбой диска здесь не должен
        # ломать остальные команды (в том числе чтение): предупреждаем и продолжаем.
        try:
            repaired = mem.repair()
        except Exception as exc:  # noqa: BLE001
            repaired = {"archives_exported": [], "error": f"{type(exc).__name__}: {exc}"}
            print(f"[repair] WARNING: derived files not restored: {exc}", file=sys.stderr)
        try:
            ctx.sync_working_file()
        except Exception as exc:  # noqa: BLE001
            print(f"[repair] WARNING: working.md not written: {exc}", file=sys.stderr)
        if repaired["archives_exported"]:
            print(f"[repair] restored archive file(s) for {repaired['archives_exported']}", file=sys.stderr)
        cmd = args.cmd
        # Операции Context проверяют аренду сами; прямые записи Memory — здесь.
        if cmd in {"commit", "add", "update", "invalidate", "link", "index"}:
            ctx._authorize()
        if cmd == "init":
            _out({"store": str(sdir), "repo": str(root), "worktree": mem.scope, "roots": list(_roots(root)),
                  "schema_version": S.SCHEMA_VERSION})
        elif cmd == "session":
            if args.action == "start":
                token = ctx.start_session(args.label)
                _out({"session": token, "worktree": mem.scope,
                      "use": f"export AGENT_MEMORY_SESSION={token}  # or pass --session {token}"})
            elif args.action == "hold":
                ctx.leases.hold(ctx.session)  # прежний держатель упразднён: понятный отказ
            elif args.action == "status":
                _out({"worktree": mem.scope, "lease": ctx.leases.status(check_owner=True)})
            elif args.action == "recover":
                _out(ctx.leases.recover(args.label, operator_confirmed=args.operator_confirmed))
            elif args.action == "adopt":
                _out(ctx.adopt())
            elif args.action == "finish":
                _out(ctx.leases.finish(ctx.session))
            else:
                ctx.leases.release(ctx.session)
                _out({"released": ctx.session})
        elif cmd == "index":
            _out(mem.index(args.paths or None, progress=args.progress, force=args.full))
        elif cmd in ("begin", "build"):
            secs = _sections(args.section)
            pack = (ctx.begin(args.task, _load(args.frame), args.budget, sections=secs) if cmd == "begin"
                    else ctx.build(args.task, _load(args.frame), args.budget, sections=secs))
            if args.json:
                _out(pack)
            else:
                head = ""
                if "episode" in pack:
                    head = f"<!-- episode {pack['episode']} · session {pack['session']}"
                    if pack.get("session_started"):
                        head += f" (new: pass --session {pack['session']} or export AGENT_MEMORY_SESSION)"
                    head += " -->\n"
                print(head + pack["markdown"])
        elif cmd == "expand":
            pack = ctx.expand(args.query, budget=args.budget, sections=_sections(args.section))
            _out(pack if args.json else pack["markdown"], args.json)
        elif cmd == "note":
            _out(ctx.note(args.text, args.kind, args.ref))
        elif cmd == "end":
            working = Path(args.working).read_text(encoding="utf-8") if args.working else None
            _out(ctx.end(_load(args.diff), summary=args.summary, working=working))
        elif cmd == "drop":
            _out(ctx.drop_episode(args.reason))
        elif cmd == "status":
            ep = ctx.current()
            _out({"worktree": mem.scope, "store": str(sdir), "lease": ctx.leases.status(check_owner=True),
                  "open_episode": ep and {k: ep[k] for k in ("id", "task", "started_at", "owner_token")},
                  "notes": len(mem.store.notes(ep["id"])) if ep else 0,
                  "working_chars": len(ctx.read_working()), "code_index": mem.code_freshness(),
                  "git": mem.view.observed(), "repaired": repaired})
        elif cmd == "repair":
            _out({**repaired, "working_synced": ctx.sync_working_file()})
        elif cmd == "commit":
            _out(mem.commit(_load(args.diff), episode=None))
        elif cmd == "search":
            _out(mem.search(args.query, args.limit, args.history, sections=_sections(args.section)))
        elif cmd == "get":
            _out(mem.get(args.id))
        elif cmd == "sources":
            _out(mem.sources(args.id))
        elif cmd == "why":
            _out(mem.why(args.id))
        elif cmd == "related":
            _out(mem.related(args.id, args.depth))
        elif cmd == "history":
            _out(mem.history(args.subject))
        elif cmd == "add":
            ep = ctx.current()
            own = ep if ep and ep.get("owner_token") == ctx.session else None
            _out(mem.add(_load(args.record), episode=own["id"] if own else None))
        elif cmd == "update":
            _out(mem.update(args.id, _load(args.patch), args.expected_version, args.reason,
                            evidence=_load(args.evidence) or []))
        elif cmd == "invalidate":
            _out(mem.invalidate(args.id, args.reason, args.expected_version, _load(args.evidence) or []))
        elif cmd == "link":
            _out(mem.link(args.src, args.rel, args.dst, origin=args.origin))
        elif cmd == "working":
            if args.action == "show":
                print(ctx.read_working())
            else:
                if not args.file:
                    raise S.ValidationError("working set needs a file")
                _out(ctx.write_working(Path(args.file).read_text(encoding="utf-8")))
        elif cmd == "compact":
            _out(ctx.compact())
        elif cmd == "maintain":
            if args.apply_safe:
                ctx._authorize()
            _out(maintenance.run(mem, apply_safe=args.apply_safe))
        elif cmd == "stats":
            _out(metrics.summary(mem.store))
        return 0
    except S.DerivedSyncError as exc:
        print(f"SAVED, DERIVED FILE PENDING: {exc}", file=sys.stderr)
        return 6
    except S.ConflictError as exc:
        print(f"CONFLICT (nothing saved): {exc}\nconflicting: {exc.conflicting}", file=sys.stderr)
        return 3
    except S.LeaseError as exc:
        print(f"LEASE (nothing saved): {exc}", file=sys.stderr)
        return 4
    except (S.MemoryError_, json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"ERROR (nothing saved): {exc}", file=sys.stderr)
        return 2
    finally:
        mem.close()


if __name__ == "__main__":
    sys.exit(main())
