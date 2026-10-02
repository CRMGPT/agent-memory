"""Проверки, которые память выполняет сама. Единственный путь к статусу verified.

Наличие файла, теста, функции или коммита утверждение не доказывает. Проверка — это
конкретный запуск с конкретным условием:

* `pytest` — запускается ровно один тест (`tests/x.py::test_y`) в этой рабочей копии;
  ожидание `pass` или `fail`. Доказывает только «этот тест сейчас проходит», а связь
  теста с утверждением агент формулирует в `condition`, и она показывается рядом.
* `pattern` — регулярное выражение ищется в конкретном фрагменте (файл и строки или
  сущность кода); ожидание `match` или `no_match`. Доказывает только наличие текста.

Хранится всё, что нужно, чтобы понять результат без доверия к пересказу: метод, условие,
ожидание, результат, хвост вывода, хэш цели, коммит и отпечаток состояния кода (правки
отслеживаемых файлов и содержимое новых неотслеживаемых), время, кто выполнил.

pytest свеж, только если одновременно: тот же HEAD, то же состояние кода (включая новые
файлы) и файл теста на месте с тем же содержимым. Состояние снимается ДО и ПОСЛЕ прогона;
если код менялся во время проверки, результат `inconclusive` — он не привязан ни к одной
версии и ничего не подтверждает. Изменился или исчез тест — needs_recheck, а не verified.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import schema as S
from .codegraph import excerpt_hash, inside_root, read_lines
from .store import now

OUTPUT_TAIL = 2000


class Prepared(dict):
    """Результат проверки, выполненной этим процессом до транзакции.

    Отдельный класс, а не флаг в словаре: JSON агента не может создать экземпляр класса,
    поэтому «готовый» результат проверки нельзя подделать во входном диффе.
    """


def prepare_ops(mem, ops: list[dict]) -> list[dict]:
    """Выполнить все проверки диффа ДО транзакции: pytest не держит блокировку общей базы."""
    import copy

    def prepared(ev: dict) -> Prepared:
        p = Prepared(run(mem, ev))
        p.request = ev  # исходный запрос: по нему считается отпечаток диффа для повтора
        return p

    def walk(evs):
        return [prepared(ev) if isinstance(ev, dict) and ev.get("kind") == "check"
                and not isinstance(ev, Prepared) else ev for ev in evs or []]

    out = []
    for op in ops:
        op = copy.deepcopy(op)
        if isinstance(op.get("evidence"), list):
            op["evidence"] = walk(op["evidence"])
        rec = op.get("record")
        if isinstance(rec, dict) and isinstance(rec.get("evidence"), list):
            rec["evidence"] = walk(rec["evidence"])
        out.append(op)
    return out


def _timeout() -> int:
    try:
        return int(os.environ.get("AGENT_MEMORY_CHECK_TIMEOUT", "600"))
    except ValueError:
        return 600


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "AGENT_MEMORY_FAULT"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # прогон не оставляет __pycache__ в проверяемом дереве
    return env


def _snapshot(mem) -> tuple[str | None, dict[str, str] | None]:
    """HEAD и состояние кода сейчас (None — git не ответил, состояние не подтверждено)."""
    mem.view.refresh()
    return mem.view.head, mem.view.code_state()


def _state_diff(a: dict[str, str] | None, b: dict[str, str] | None) -> list[str]:
    a, b = a or {}, b or {}
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def _file_hash(root: Path, rel: str) -> str | None:
    p = inside_root(root, rel)
    if p is None or not p.is_file():
        return None
    return excerpt_hash(p.read_text(encoding="utf-8", errors="replace"))


def _pattern_target(mem, ev: dict) -> tuple[str | None, str]:
    """Текст фрагмента для pattern-проверки и его описание."""
    from .memory import parse_locator

    ref = ev["ref"]
    as_file = inside_root(mem.root, ref.removeprefix("file:"))
    if ref.startswith("code:") or as_file is None or not as_file.is_file():
        try:
            node_id = mem.resolve_target(ref, allow_create=False)
        except S.MemoryError_:
            return None, ref
        node = mem.store.current_code_node(node_id)
        if not node or node["status"] != "present":
            return None, ref
        return read_lines(mem.root, node["path"], node["start_line"], node["end_line"], max_lines=None), node["id"]
    rng = parse_locator(ev.get("locator"))
    return read_lines(mem.root, ref.removeprefix("file:"), *(rng or (None, None)), max_lines=None), ref


def run(mem, ev: dict) -> dict:
    """Выполнить проверку и вернуть поля доказательства. Неверный ввод — ValidationError."""
    method = ev.get("method") or ev.get("check_method")
    if method not in S.CHECK_METHODS:
        raise S.ValidationError(f"check method {method!r} unknown; use one of {S.CHECK_METHODS}")
    condition = (ev.get("condition") or "").strip()
    if not condition:
        raise S.ValidationError("check needs condition: what exactly this check establishes about the claim")
    ref = (ev.get("ref") or "").strip()
    if method == "pytest" or not ref.startswith("code:"):
        target_path = ref.split("::", 1)[0].removeprefix("file:")
    else:
        try:
            target_path = mem._node_path(mem.resolve_target(ref, allow_create=False))
        except S.MemoryError_:
            target_path = None
    mem.refuse_ignored([target_path], "check target")  # до запуска: игнорируемое не читаем и не хэшируем
    started = time.monotonic()
    head, state = _snapshot(mem)  # проверка привязывается к точному текущему состоянию кода
    out: dict = {"kind": "check", "category": "check", "check_method": method, "check_condition": condition,
                 "check_commit": head, "check_dirty": int(bool(head) and bool(state)),
                 "check_patch": json.dumps(state, sort_keys=True) if state is not None else None,
                 "checked_at": now()}

    if method == "pytest":
        expect = ev.get("expect", "pass")
        if expect not in ("pass", "fail"):
            raise S.ValidationError("pytest check expect must be pass or fail")
        path = ref.split("::", 1)[0]
        if "::" not in ref or inside_root(mem.root, path) is None or not (mem.root / path).is_file():
            raise S.ValidationError(f"pytest check needs one test node id inside the repo, like "
                                    f"tests/test_x.py::test_y; got {ref!r}")
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", ref]
        try:
            proc = subprocess.run(cmd, cwd=mem.root, capture_output=True, encoding="utf-8", errors="replace", timeout=_timeout(),
                                  env=_clean_env(), stdin=subprocess.DEVNULL)
            output, code = (proc.stdout + proc.stderr), proc.returncode
        except subprocess.TimeoutExpired as exc:
            output, code = f"timeout after {exc.timeout} s", -1
        if code == 0:
            outcome = "pass"
        elif code == 1:
            outcome = "fail"
        else:  # 2-5: прерван, ошибка сбора, тест не найден — это не «упал», это «не проверено»
            outcome = "error"
        head_after, state_after = _snapshot(mem)
        moved = _state_diff(state, state_after)
        if head and (state is None or state_after is None or head_after != head or moved):
            # Код менялся во время прогона: результат не относится ни к версии «до», ни «после».
            what = (f"HEAD {head[:10]} -> {(head_after or '?')[:10]}" if head_after != head
                    else "code state not confirmed by git" if state is None or state_after is None
                    else f"changed: {', '.join(moved[:10])}")
            banner = f"[agent_memory] code changed while the check ran ({what}); result {outcome} discarded\n"
            output = banner + output[-(OUTPUT_TAIL - len(banner)):]
            outcome = "inconclusive"
        out.update(ref=ref, check_expect=expect, check_result=outcome,
                   check_output=output[-OUTPUT_TAIL:], check_target_hash=_file_hash(mem.root, path),
                   check_origin=f"executed by agent_memory: {' '.join(cmd[2:])} (exit {code}, "
                                f"{time.monotonic() - started:.1f} s)")
        return out

    pattern = ev.get("pattern")
    expect = ev.get("expect", "match")
    if not pattern:
        raise S.ValidationError("pattern check needs pattern (a regular expression)")
    if expect not in ("match", "no_match"):
        raise S.ValidationError("pattern check expect must be match or no_match")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise S.ValidationError(f"pattern {pattern!r} is not a valid regular expression: {exc}") from exc
    text, target = _pattern_target(mem, ev)
    if text is None:
        raise S.ValidationError(f"pattern check target {ref!r} not found in this worktree")
    found = bool(rx.search(text))
    outcome = "pass" if found == (expect == "match") else "fail"
    out.update(ref=target, locator=ev.get("locator"), check_expect=f"{expect}:{pattern}", check_result=outcome,
               check_output=(rx.search(text).group(0) if found else "no match")[:OUTPUT_TAIL],
               check_target_hash=excerpt_hash(text),
               check_origin="executed by agent_memory: regex search in the target text")
    return out


def state(mem, ev: dict) -> dict:
    """Состояние сохранённой проверки сейчас.

    pytest — результат относится только к тому состоянию кода, на котором тест запускали:
    тот же HEAD, те же незакоммиченные правки и те же новые файлы, а файл теста на месте и
    не изменился. Любой другой код (новый коммит, правка, новый или изменённый локальный
    файл, другая рабочая копия), исчезнувший тест или неподтверждённое состояние git —
    проверка устарела, нужен повторный запуск. `inconclusive` (код менялся во время прогона)
    не свеж никогда.
    pattern — дешёвая проверка, её пересчитываем сейчас на текущем тексте.

    level: verified — свежий прогон одного теста с ожиданием pass; check_passed — свежая
    проверка другого вида (шаблон в тексте, ожидаемое падение теста): подтверждает только
    своё условие; needs_recheck — проверка устарела или не проходит.
    """
    method = ev.get("check_method")
    result = ev.get("check_result")
    expect_full = ev.get("check_expect") or ""
    expect = expect_full.split(":", 1)[0]
    if method == "pattern":
        text, _ = _pattern_target(mem, ev) if ev.get("ref") else (None, None)
        current = excerpt_hash(text) if text is not None else None
        pattern = expect_full.split(":", 1)[1] if ":" in expect_full else ""
        now_found = bool(text is not None and pattern and re.search(pattern, text))
        live = "pass" if now_found == (expect == "match") else "fail"
        satisfied = text is not None and live == "pass"
        fresh = True  # пересчитано сейчас
        result = live if text is not None else "missing"
    else:
        current = _file_hash(mem.root, (ev.get("ref") or "").split("::", 1)[0])
        satisfied = result == expect
    if current is None:
        target = "missing"
    elif current == ev.get("check_target_hash"):
        target = "unchanged"
    else:
        target = "changed"
    note = None
    if method != "pattern":
        try:
            recorded = json.loads(ev["check_patch"]) if ev.get("check_patch") is not None else None
        except (TypeError, ValueError):
            recorded = None
        now_state = mem.view.code_state()
        fresh = (bool(ev.get("check_commit")) and ev.get("check_commit") == mem.view.head
                 and recorded is not None and now_state is not None and recorded == now_state
                 and target == "unchanged" and result != "inconclusive")
        if result == "inconclusive":
            note = "code changed while the check ran; rerun it on a stable tree"
        elif target != "unchanged":
            note = f"test file {target} since the run"
    if satisfied and fresh and method == "pytest" and expect == "pass":
        level = "verified"
    elif satisfied and fresh:
        level = "check_passed"
    else:
        level = "needs_recheck"
    return {"method": method, "condition": ev.get("check_condition"), "result": result,
            "satisfied": satisfied, "fresh": fresh, "target": target, "level": level,
            "passed_and_fresh": satisfied and fresh, "note": note,
            "commit": ev.get("check_commit"), "dirty": bool(ev.get("check_dirty")),
            "checked_at": ev.get("checked_at"), "origin": ev.get("check_origin")}


LEVEL_ORDER = ("verified", "check_passed", "needs_recheck")


def best_level(mem, evs: list[dict]) -> str | None:
    """Сильнейший уровень среди проверок записи; None — проверок нет."""
    levels = [state(mem, e)["level"] for e in evs if e.get("kind") == "check"]
    if not levels:
        return None
    return min(levels, key=LEVEL_ORDER.index)
