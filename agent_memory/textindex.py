"""Токенизация и векторное сходство без внешних зависимостей.

Вектор — хэширование слов и символьных триграмм в 256 измерений. Это не
нейросетевой эмбеддинг: он ловит общие корни и части идентификаторов
(rotate_token ~ token rotation), но не синонимы. Интерфейс Embedder позволяет
подставить настоящую модель, не трогая хранилище и поиск.
"""

from __future__ import annotations

import hashlib
import math
import re
from array import array
from typing import Protocol

DIM = 256

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")

STOPWORDS = frozenset(
    """
    a an the and or of to in on for with without from by at as is are was were be been being it its this that
    these those into over under not no do does did done we you they i he she our your their them us can could
    should would will must may might shall fix make add use using need needs new old when then than so if else
    also just only about after before during while how what why which who where task bug issue work
    и в во на с со по к ко у о об от до за из для не ни но а же ли что как это то при над под без
    """.split()
)


def split_identifier(text: str) -> list[str]:
    """`AuthService.rotate_token` -> [auth, service, rotate, token]."""
    out: list[str] = []
    for word in _WORD.findall(text):
        for part in _CAMEL.sub(" ", word).split():
            for piece in part.split("_"):
                if piece:
                    out.append(piece.lower())
    return out


def _fold(t: str) -> str:
    """Лёгкое сведение английского множественного числа: tokens -> token, policies -> policy."""
    if not t.isascii() or len(t) <= 3:
        return t
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def tokens(text: str) -> list[str]:
    return [_fold(t) for t in split_identifier(text) if t not in STOPWORDS and len(t) > 1]


def normalize_statement(text: str) -> str:
    """Ключ точного дубля: не различаются только регистр, пробелы и точка в конце.

    Знаки сравнения, отрицания и числа с точкой несут смысл: «> 5 s» и «< 5 s» — разные утверждения.
    """
    return " ".join(text.lower().split()).rstrip(" .;")


def estimate_tokens(text: str) -> int:
    """Оценка токенов без токенизатора конкретного провайдера: ~4 символа на токен."""
    return max(1, (len(text) + 3) // 4)


class Embedder(Protocol):
    def embed(self, text: str) -> array: ...


class HashingEmbedder:
    def embed(self, text: str) -> array:
        vec = [0.0] * DIM
        toks = tokens(text)
        feats: list[tuple[str, float]] = [(t, 1.0) for t in toks]
        for t in toks:
            padded = f"#{t}#"
            for i in range(len(padded) - 2):
                feats.append(("3:" + padded[i : i + 3], 0.35))
        for feat, weight in feats:
            h = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % DIM
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return array("f", (v / norm for v in vec))


def cosine(a: array, b: array) -> float:
    return sum(x * y for x, y in zip(a, b))


def to_blob(vec: array) -> bytes:
    return vec.tobytes()


def from_blob(blob: bytes) -> array:
    vec = array("f")
    vec.frombytes(blob)
    return vec


def fts_query(terms: list[str]) -> str:
    """Запрос FTS5: OR по терминам в кавычках, чтобы спецсимволы не ломали синтаксис."""
    seen: list[str] = []
    for term in terms:
        for t in tokens(term):
            if t not in seen:
                seen.append(t)
    return " OR ".join('"' + t.replace('"', "") + '"' for t in seen[:40])
