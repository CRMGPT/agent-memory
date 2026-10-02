"""Словарь памяти: типы записей, статусы, отношения графа, виды доказательств.

Здесь только перечисления и правила допустимости. Хранилище и логика живут
в store.py / memory.py, чтобы правила можно было прочитать в одном месте.
"""

from __future__ import annotations

# Версия схемы общей базы знаний. 1 — первая схема, 2 — текущая:
# производное состояние кода вынесено в базу рабочей копии, проверки, область применимости,
# аренда записи, канонический архив эпизода в базе.
SCHEMA_VERSION = 2

RECORD_TYPES = (
    "FACT",
    "DECISION",
    "HYPOTHESIS",
    "OBSERVATION",
    "CONSTRAINT",
    "BUG",
    "QUESTION",
    "EPISODE",
    "DOCUMENT",
    "TEST_RESULT",
)

# Разделы памяти: признак записи, а не отдельная база.
#   code     — устройство проекта: модули, связи, точки входа;
#   logic    — логика и решения: правила поведения, причины решений, ограничения;
#   data     — структура данных и источники: схемы, форматы, происхождение и смысл полей
#              (без клиентских записей);
#   workflow — процесс работы: запуск, тесты, доставка, устойчивые рабочие правила.
# Запись прежней версии без раздела — «не классифицирована» (UNCLASSIFIED): это техническое
# состояние перехода, а не пятый раздел; её раздел задаёт UPDATE patch {"section": ...}.
SECTIONS = ("code", "logic", "data", "workflow")
UNCLASSIFIED = "unclassified"

# Префикс стабильного идентификатора: decision_00483, fact_00012 ...
# Итог эпизода — episode_summary_00007, чтобы не путать с самим эпизодом episode_000007.
ID_PREFIX = {t: t.lower() for t in RECORD_TYPES} | {"EPISODE": "episode_summary"}

# Жизненный цикл записи. Меняют его только операции диффа, и действует перемена там,
# где её коммит входит в историю рабочей копии (см. view.py).
STATUSES = ("active", "superseded", "invalidated", "resolved")
TRANSITION_OF = {"SUPERSEDE": "superseded", "INVALIDATE": "invalidated", "RESOLVE": "resolved"}

# Насколько утверждение подкреплено. Порядок — от сильного к слабому.
#   verified      — память сама прогнала один pytest-тест с ожиданием pass на ТОМ ЖЕ состоянии кода
#                   (HEAD и незакоммиченные правки), что сейчас;
#   check_passed  — прошла свежая проверка другого вида: шаблон в тексте или ожидаемое падение теста.
#                   Подтверждает только своё условие, а не утверждение целиком;
#   needs_recheck — проверка была, но код с тех пор другой или она не проходит;
#   sourced       — есть доступный проверяемый источник (файл, код, коммит), но само утверждение не проверено;
#   attested      — засвидетельствовано: слово владельца, вывод тула, внешний документ с цитатой;
#   probable / hypothesis / unknown — объявлено агентом без опоры;
#   contradicted / superseded — следствие INVALIDATE / SUPERSEDE.
CERTAINTIES = (
    "verified",
    "check_passed",
    "needs_recheck",
    "sourced",
    "attested",
    "probable",
    "hypothesis",
    "unknown",
    "contradicted",
    "superseded",
)
# Что агент может объявить сам. verified и needs_recheck выводятся из проверок, остальное — из статуса.
DECLARABLE_CERTAINTIES = ("sourced", "attested", "probable", "hypothesis", "unknown")

# Виды доказательств и их категории.
#   verifiable — память сама находит источник и сверяет фрагмент: файл и строки, сущность кода, коммит;
#   attested   — вывод тула и слово владельца: только с текстом (note), ссылка на исходный вывод лучше пересказа;
#   external   — веб-страница: только с цитатой, датой обращения и, если важна, версией;
#   check      — проверка, которую выполнила сама память (pytest-тест или шаблон в коде);
#   other      — эпизод; model — вывод самой модели, опорой не считается.
EVIDENCE_KINDS = ("file", "code", "test", "doc", "commit", "tool", "user", "url", "check", "episode", "model")
EVIDENCE_CATEGORY = {
    "file": "verifiable", "code": "verifiable", "test": "verifiable", "doc": "verifiable", "commit": "verifiable",
    "tool": "attested", "user": "attested",
    "url": "external",
    "check": "check",
    "episode": "other",
    "model": "model",
}
CHECK_METHODS = ("pytest", "pattern")

# Записи, которые без доказательства не принимаются вовсе.
NEEDS_EVIDENCE = frozenset({"FACT", "DECISION", "CONSTRAINT", "BUG", "OBSERVATION", "TEST_RESULT"})
# Записи, которые нельзя держать только на выводе модели.
NEEDS_NON_MODEL_EVIDENCE = frozenset({"FACT", "CONSTRAINT", "TEST_RESULT"})
# Записи, у которых одинаковый subject с другой формулировкой — противоречие.
CONTRADICTION_TYPES = frozenset({"FACT", "DECISION", "CONSTRAINT"})
# Какие записи можно закрыть операцией RESOLVE.
RESOLVABLE = frozenset({"BUG", "QUESTION", "HYPOTHESIS"})

# Отношения графа. Код — детерминированные, остальное — смысловые.
CODE_RELATIONS = ("CALLS", "IMPORTS", "IMPLEMENTS", "DEPENDS_ON", "TESTED_BY", "DEFINED_IN", "RENAMED_TO")
SEMANTIC_RELATIONS = (
    "AFFECTS",
    "RELATED_TO",
    "CONSTRAINED_BY",
    "DECIDED_BY",
    "SUPPORTED_BY",
    "CONTRADICTS",
    "SUPERSEDES",
    "CAUSED_BY",
    "RESOLVES",
    "MENTIONED_IN",
    "DISCOVERED_IN",
)
RELATIONS = CODE_RELATIONS + SEMANTIC_RELATIONS

# Происхождение ребра: из анализа кода, заявлено агентом с доказательством, выведено моделью.
EDGE_ORIGINS = ("deterministic", "asserted", "inferred")

# Вес отношения при расширении графа. Смысловые связи с кодом важнее структурных.
RELATION_WEIGHT = {
    "AFFECTS": 1.0,
    "CONSTRAINED_BY": 1.0,
    "DECIDED_BY": 0.9,
    "SUPPORTED_BY": 0.8,
    "CONTRADICTS": 0.9,
    "SUPERSEDES": 0.5,
    "CAUSED_BY": 0.9,
    "RESOLVES": 0.8,
    "RENAMED_TO": 0.9,
    "TESTED_BY": 0.6,
    "DEFINED_IN": 0.5,
    "CALLS": 0.5,
    "IMPORTS": 0.3,
    "IMPLEMENTS": 0.6,
    "DEPENDS_ON": 0.5,
    "RELATED_TO": 0.4,
    "MENTIONED_IN": 0.3,
    "DISCOVERED_IN": 0.2,
}

# Множитель ранжирования по подкреплённости.
CERTAINTY_WEIGHT = {
    "verified": 1.0,
    "check_passed": 0.95,
    "needs_recheck": 0.85,
    "sourced": 0.9,
    "attested": 0.85,
    "probable": 0.8,
    "hypothesis": 0.65,
    "unknown": 0.5,
    "contradicted": 0.2,
    "superseded": 0.2,
}

# Тип записи, который особенно важен рядом с кодом: ограничения и решения.
TYPE_WEIGHT = {
    "CONSTRAINT": 1.2,
    "DECISION": 1.15,
    "BUG": 1.1,
    "FACT": 1.0,
    "TEST_RESULT": 0.9,
    "HYPOTHESIS": 0.85,
    "QUESTION": 0.8,
    "OBSERVATION": 0.8,
    "DOCUMENT": 0.7,
    "EPISODE": 0.5,
}

WORKING_SECTIONS = (
    "Goal",
    "Current task",
    "Current state",
    "Important constraints",
    "Active hypotheses",
    "Decisions relevant to current work",
    "Open questions",
    "Current plan",
)


class MemoryError_(Exception):
    """Ошибка памяти, которую агент должен прочитать и исправить вход."""


class ValidationError(MemoryError_):
    pass


class ConflictError(MemoryError_):
    """Новая запись спорит с действующей или запись изменилась после чтения."""

    def __init__(self, message: str, conflicting: list[str]):
        super().__init__(message)
        self.conflicting = conflicting


class NotFound(MemoryError_):
    pass


class LeaseError(MemoryError_):
    """Рабочую копию держит другая сессия, или сессия не указана."""


class MigrationRequired(MemoryError_):
    """Хранилище старой версии: новый код не пишет в него, пока владелец не выполнит migrate."""


class DerivedSyncError(MemoryError_):
    """База сохранена, но производный файл (архив, working.md) не записан. Чинит `repair`."""
