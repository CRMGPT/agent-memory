"""Понимание задачи до поиска: что искать, а не как решать.

Основной путь — агент сам пишет рамку задачи (JSON с полями ниже) и передаёт
её в `begin --frame`. Эвристика здесь — запасной вариант без модели: она
вытаскивает пути, идентификаторы и значимые слова. Рамки складываются:
поля агента дополняют эвристику, а не заменяют её.
"""

from __future__ import annotations

import re

from .textindex import STOPWORDS, tokens

FIELDS = ("task", "entities", "concepts", "files", "systems", "constraints", "search_terms")

_PATH = re.compile(r"(?:[\w.-]+/)+[\w.-]+|\b[\w-]+\.(?:py|md|ts|js|sql|ya?ml|json|sh|toml)\b")
_IDENT = re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*(?:\(\))?")


def _is_identifier(word: str) -> bool:
    w = word.rstrip("()")
    return ("_" in w or "." in w or bool(re.search(r"[a-z][A-Z]", w)) or word.endswith("()")) and len(w) > 2


def heuristic_frame(task: str) -> dict:
    files = sorted({m.group(0) for m in _PATH.finditer(task)})
    rest = _PATH.sub(" ", task)
    entities = []
    for m in _IDENT.finditer(rest):
        w = m.group(0)
        if _is_identifier(w) and w.rstrip("()") not in entities:
            entities.append(w.rstrip("()"))
    words = [t for t in tokens(rest) if t not in STOPWORDS]
    concepts = []
    for w in words:
        if w not in concepts:
            concepts.append(w)
    bigrams = []
    plain = [w.lower() for w in re.findall(r"[A-Za-zА-Яа-яЁё]+", rest) if w.lower() not in STOPWORDS]
    for a, b in zip(plain, plain[1:]):
        bg = f"{a} {b}"
        if bg not in bigrams:
            bigrams.append(bg)
    return {
        "task": task.strip(),
        "entities": entities,
        "concepts": concepts,
        "files": files,
        "systems": [],
        "constraints": [],
        "search_terms": bigrams[:8] + concepts[:20],
    }


def merge_frames(base: dict, extra: dict | None) -> dict:
    out = {k: (list(base.get(k) or []) if k != "task" else base.get("task", "")) for k in FIELDS}
    for k, v in (extra or {}).items():
        if k == "task" and v:
            out["task"] = v
        elif k in FIELDS and isinstance(v, list):
            for item in v:
                if item and item not in out[k]:
                    out[k].append(item)
    return out


def build_frame(task: str, extra: dict | None = None) -> dict:
    return merge_frames(heuristic_frame(task), extra)


def frame_terms(frame: dict) -> list[str]:
    """Все поисковые слова рамки одним списком, без повторов."""
    out: list[str] = []
    for k in ("search_terms", "entities", "concepts", "systems", "constraints", "files"):
        for item in frame.get(k) or []:
            if item not in out:
                out.append(item)
    return out


def frame_anchors(frame: dict) -> list[str]:
    """Точные якоря: имена кода и файлы. По ним ищем узлы графа, а не текст."""
    return [*frame.get("files", []), *frame.get("entities", [])]
