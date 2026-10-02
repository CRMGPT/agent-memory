"""Искусственные сбои для проверок восстановления.

`AGENT_MEMORY_FAULT=<точка>` — исключение в этой точке, как отказ диска или COMMIT.
`AGENT_MEMORY_FAULT=<точка>:exit` — процесс обрывается без очистки (os._exit), как kill -9.
В обычной работе переменная не задана, и вызовы ничего не делают.
"""

from __future__ import annotations

import os

POINTS = (
    "end_before_commit",   # внутри транзакции end, до записи
    "end_commit",          # ровно перед COMMIT транзакции end
    "end_after_commit",    # база сохранена, производные файлы ещё не трогали
    "export_archive",      # запись JSON-архива эпизода
    "write_working",       # запись файла working.md
    "recover_commit",      # ровно перед COMMIT передачи аренды (session recover)
    "end_reindex",         # end: обновление карты кода своей копии перед проверкой ссылок
    "finish_after_release",  # session finish: аренда снята, свои держатели ещё не остановлены
)


class InjectedFault(RuntimeError):
    pass


def hit(point: str) -> None:
    spec = os.environ.get("AGENT_MEMORY_FAULT", "")
    if not spec:
        return
    name, _, mode = spec.partition(":")
    if name != point:
        return
    if mode == "exit":
        os._exit(91)
    raise InjectedFault(f"injected fault at {point}")
