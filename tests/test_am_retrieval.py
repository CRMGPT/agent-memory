"""Поиск среди тысячи посторонних записей и «забытая деталь», которой нет в working.md."""

from __future__ import annotations

import random

import pytest

from agent_memory.retrieval import Retriever
from agent_memory.taskframe import build_frame

ROTATE = "code:app/auth/service.py::AuthService.rotate_token"

TOPICS = ["billing invoice", "csv export", "dashboard chart", "email template", "pdf report", "cron schedule",
          "image resize", "search ranking", "currency rounding", "locale format", "webhook retry",
          "cache eviction", "pagination cursor", "feature flag", "audit log", "rate limiter"]


def _noise(mem, n: int, seed: int = 7) -> None:
    rnd = random.Random(seed)
    ops = []
    for i in range(n):
        topic = rnd.choice(TOPICS)
        ops.append({"op": "ADD", "record": {
            "type": rnd.choice(["FACT", "OBSERVATION", "DECISION"]),
            "section": rnd.choice(["code", "logic", "data", "workflow"]),
            "statement": f"{topic} module variant {i} keeps setting {rnd.randint(1, 10**6)} for tenant {i % 37}",
            "evidence": [{"kind": "tool", "ref": f"run-{i}", "note": "log line"}],
            "coexist": True}})
    mem.commit({"ops": ops})


@pytest.mark.parametrize("noise", [1000])
def test_relevant_facts_found_among_thousand_unrelated(mem, ctx, noise):
    _noise(mem, noise)
    needed = mem.commit({"ops": [
        {"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": "Refresh tokens are stored in Redis with a 30 day TTL",
                                 "evidence": [{"kind": "doc", "ref": "docs/auth.md"}]}},
        {"op": "ADD", "record": {"type": "CONSTRAINT", "section": "logic",
                                 "statement": "Token rotation must revoke the old refresh token in the same step",
                                 "evidence": [{"kind": "user", "ref": "owner", "note": "security requirement"}]}},
        {"op": "ADD", "record": {"type": "DECISION", "section": "logic", "statement": "Refresh endpoint is rate limited per user, "
                                                                 "not per IP",
                                 "evidence": [{"kind": "file", "ref": "app/api/refresh.py"}]}},
    ]})["added"]
    total = mem.store.count_records()
    assert total == noise + 3

    pack = ctx.build("Change refresh token rotation and revoke policy for the refresh endpoint")
    for rid in needed:
        assert rid in pack["injected"], (rid, pack["markdown"][:3000])
    # Контекст несёт крошечную долю памяти, а не «всё на всякий случай».
    assert len(pack["injected"]) <= 25
    assert len(pack["injected"]) / total < 0.03
    assert pack["pack_tokens_est"] <= 6000
    assert pack["stats"]["nodes_injected"] == len(pack["injected"])


def test_forgotten_constraint_recovered_through_code_graph(mem, ctx):
    # Эпизод 1 записал ограничение и привязал его к коду. В working.md о нём ни слова.
    ctx.begin("harden auth")
    ctx.end({"ops": [{"op": "ADD", "record": {
        "type": "CONSTRAINT", "section": "logic", "statement": "Never hold the Redis lock longer than 50 ms; the gateway times out",
        "evidence": [{"kind": "user", "ref": "owner", "note": "said in chat"}],
        "links": [{"rel": "AFFECTS", "to": ROTATE}]}}]})
    for i in range(40):  # много эпизодов спустя
        ctx.begin(f"unrelated work {i} on csv export")
        ctx.end({"ops": [{"op": "ADD", "record": {"type": "OBSERVATION", "section": "workflow",
                                                  "statement": f"csv export column {i} is padded",
                                                  "evidence": [{"kind": "tool", "ref": f"r{i}", "note": "csv diff"}]}}]})
    assert "Redis lock" not in ctx.read_working()

    # Задача не упоминает ни Redis, ни блокировку, ни таймаут — только код.
    frame = build_frame("Refactor rotate_token for readability")
    lexical_only = {h["id"] for h in mem.search("Refactor rotate_token for readability", limit=20)}
    assert "constraint_00001" not in lexical_only  # одним сходством текста не найти

    pack = ctx.build("Refactor rotate_token for readability")
    assert "constraint_00001" in pack["injected"]
    assert "pinned: constraint AFFECTS" in pack["markdown"]
    sel = Retriever(mem).retrieve(frame)
    assert sel.stats["graph_hops"] >= 1


def test_graph_expansion_reaches_constraint_two_hops_away(mem, ctx):
    # Ограничение висит на функции, которую вызывает код из задачи: refresh -> rotate_token.
    mem.commit({"ops": [{"op": "ADD", "record": {
        "type": "CONSTRAINT", "section": "logic", "statement": "Issued token format is opaque; clients must not parse it",
        "evidence": [{"kind": "user", "ref": "owner", "note": "said in chat"}],
        "links": [{"rel": "AFFECTS", "to": ROTATE}]}}]})
    pack = ctx.build("Speed up app/api/refresh.py handler refresh()")
    assert "constraint_00001" in pack["injected"]
    assert any("CALLS" in r for r in pack["markdown"].splitlines() if "Why included" in r) or \
        "CALLS" in pack["markdown"]


def test_lazy_expand_does_not_repeat_injected(mem, ctx):
    mem.commit({"ops": [
        {"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": "Login issues tokens prefixed with tok-",
                                 "evidence": [{"kind": "code", "ref": "AuthService._issue"}]}},
        {"op": "ADD", "record": {"type": "FACT", "section": "code", "statement": "Gateway timeout for auth calls is 2 seconds",
                                 "evidence": [{"kind": "tool", "ref": "gateway config dump", "note": "timeout: 2s"}]}},
    ]})
    first = ctx.begin("Change token prefix produced by _issue")
    assert "fact_00001" in first["injected"]
    assert "fact_00002" not in first["injected"]
    page = ctx.expand("gateway timeout for auth")
    assert page["injected"] == ["fact_00002"]
    again = ctx.expand("gateway timeout for auth")
    assert again["injected"] == []  # уже в контексте — второй раз не грузим
    assert len(mem.store.metrics("expand")) == 2


def test_file_anchor_pins_constraint_regardless_of_fanout(mem, ctx, put):
    funcs = "\n\n".join(f"def step_{i:02d}(x):\n    return x + {i}\n" for i in range(30))
    put("app/pipeline.py", funcs)
    mem.index()
    mem.add({"type": "CONSTRAINT", "section": "logic", "statement": "step_29 output is persisted; never change its return type",
             "evidence": [{"kind": "user", "ref": "owner", "note": "said in chat"}],
             "links": [{"rel": "AFFECTS", "to": "step_29"}]})
    pack = ctx.build("Tidy up app/pipeline.py")
    assert pack["injected"] == ["constraint_00001"]
    assert "pinned: constraint AFFECTS step_29" in pack["markdown"]
    # Тело грузится только у привязанной к памяти функции; остальные — строкой в соседстве.
    assert pack["code_injected"] == ["code:app/pipeline.py::step_29"]
