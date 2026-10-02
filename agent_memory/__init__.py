"""Постоянная память агента-разработчика.

Контекстное окно — рабочая память, а не долговременная. Долговременное знание
лежит здесь: записи с доказательствами, граф связей с кодом, архив эпизодов.
В контекст попадает только то, что нужно текущей задаче.

Устройство — README.md и docs/design.md. Процедура для агента — скилл
skills/project-memory/SKILL.md (в Claude Code: agent-memory:project-memory).
"""

from .context import Context
from .memory import Memory
from .schema import (
    ConflictError,
    DerivedSyncError,
    LeaseError,
    MigrationRequired,
    NotFound,
    ValidationError,
)

__all__ = ["Memory", "Context", "ConflictError", "DerivedSyncError", "LeaseError", "MigrationRequired",
           "NotFound", "ValidationError"]
