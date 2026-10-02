"""Сотни эпизодов: память растёт, активный контекст остаётся ограниченным, полезное находится."""

from __future__ import annotations

import random

from agent_memory import metrics

SUBSYSTEMS = ["billing", "export", "dashboard", "mailer", "scheduler", "uploader", "search", "reports",
              "webhooks", "cache", "pagination", "flags", "audit", "ratelimit", "auth", "sessions"]


def test_three_hundred_episodes_keep_context_bounded(mem, ctx):
    rnd = random.Random(11)
    planted = {}
    pack_tokens = []
    for ep in range(300):
        sub = SUBSYSTEMS[ep % len(SUBSYSTEMS)]
        pack = ctx.begin(f"episode {ep}: change {sub} retry behaviour and logging")
        pack_tokens.append(pack["pack_tokens_est"])
        for n in range(20):
            ctx.note(f"{sub} tool output {n}: " + "lorem " * 50, kind="tool_result")
        ops = [{"op": "ADD", "record": {
            "type": "OBSERVATION", "section": "workflow", "statement": f"{sub} job {ep} took {rnd.randint(10, 999)} ms on run {ep}",
            "evidence": [{"kind": "tool", "ref": f"run-{ep}", "note": "timer output"}]}}]
        if ep in (5, 120, 240):
            statement = {5: "Billing retries must be idempotent by invoice id",
                         120: "Mailer batches never exceed 500 recipients per provider call",
                         240: "Scheduler runs in UTC; local times are converted at the edge"}[ep]
            ops.append({"op": "ADD", "record": {"type": "CONSTRAINT", "section": "logic", "statement": statement,
                                                "evidence": [{"kind": "user", "ref": "owner", "note": "said in chat"}]}})
        res = ctx.end({"ops": ops})
        for rid in res["commit"]["added"]:
            if rid.startswith("constraint"):
                planted[ep] = rid

    assert mem.store.count_records() >= 303
    # Активный контекст не растёт вместе с памятью.
    early, late = max(pack_tokens[:30]), max(pack_tokens[-30:])
    assert late <= 6000 and late < early * 3 + 1500
    # Старые ограничения находятся по смыслу задачи.
    assert planted[5] in ctx.build("make billing retries safe to repeat for one invoice")["injected"]
    assert planted[120] in ctx.build("mailer batches size per provider call")["injected"]
    assert planted[240] in ctx.build("scheduler time zone handling for local times")["injected"]

    s = metrics.summary(mem.store)
    assert s["memory_commits"] == 300 and s["episodes"]["committed"] == 300
    # Это проверка хранения и поиска, не выполнения задач агентом: память не видит его контекста.
    assert s["working_tokens_est_after_end"]["max"] < s["episode_notes_tokens_est"]["min"]
    assert s["nodes_injected"]["max"] <= 40
    assert s["retrieval_latency_ms"]["avg"] < 2000
    # Временные заметки 300 эпизодов в рабочей базе не остались.
    assert mem.store.conn.execute("select count(*) from episode_notes").fetchone()[0] == 0
    assert len(list((mem.dir / "episodes").glob("*.json"))) == 300
