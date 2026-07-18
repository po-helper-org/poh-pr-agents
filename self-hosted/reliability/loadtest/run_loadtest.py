#!/usr/bin/env python3
"""Синтетический нагрузочный прогон ядра надёжности (100k+ событий, stdlib-only).

Гоняет НАСТОЯЩИЙ код конвейера — ingress → durable queue → worker(process/supervisor)
→ sweeper — через большой поток синтетических webhook'ов. Замоканы только внешние
зависимости (LLM Z.AI и GitHub API); проверяется само ядро под нагрузкой и конкуренцией.

Топология как в проде на одном узле Dokploy: общий SQLite на диске (state + queue),
пул воркеров, пул продюсеров webhook'ов. Каждый поток открывает СВОИ соединения к тем
же файлам — точная модель отдельных процессов ingress/worker на общем volume.

Проверяем инварианты контракта (см. SYSTEM-REQUIREMENTS.md):
  К-1  нет тихих потерь      — каждое принятое событие в терминале (DONE|DEAD_LETTER)
  СТ-2  dedup               — повторная доставка (тот же delivery_id) не плодит строк
  СТ-16 идемпотентность      — не более одного DONE на бизнес-ключ
  СТ-6/9 durable queue       — очередь полностью осушается; DLQ = ровно перманентные сбои
  СТ-27 не молчать           — каждый dead-letter даёт ровно один видимый коммент
  захваты                    — таблица claims пуста (нет утёкших захватов, К-1)
  учёт                       — done + dead_letter == принято; принято == 2×webhooks (dedup)
и меряем throughput (соб/с) + латентность p50/p95/p99 (по created_at→updated_at стора).

Фаза B отдельно проверяет reconciliation sweeper (СТ-13/29/32): пропущенный webhook,
застрявший воркер и «отравленное» событие — все доводятся до корректного терминала.

Запуск:
    cd self-hosted && python3 reliability/loadtest/run_loadtest.py --events 100500
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time

# --- self-hosted/ в sys.path, чтобы `import reliability...` работал при прямом запуске ---
_SELF_HOSTED = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SELF_HOSTED not in sys.path:
    sys.path.insert(0, _SELF_HOSTED)

from reliability import metrics
from reliability.ingress import handle_webhook
from reliability.queue import DurableQueue
from reliability.state import (
    Backpressure,
    Event,
    State,
    StateStore,
    event_to_dict,
)
from reliability.sweeper import OpenPR, StuckTimeout, sweep
from reliability.sweeper_adapter import make_has_completed_review
from reliability.worker import run_once

COMMANDS_PER_WEBHOOK = 2  # parse_events по умолчанию даёт /describe и /review


# ───────────────────────── моки внешних зависимостей ─────────────────────────
class FakeAnalyze:
    """Замена вызова pr-agent+Z.AI. Детерминированно по бизнес-ключу назначает
    исход, чтобы ретраи вели себя стабильно:
      perm  — всегда падает  → уходит в DLQ (проверяем СТ-27/эскалацию);
      flaky — падает первые N попыток, потом успех (ретрай/backoff, СТ-12);
      bp    — сигналит Backpressure первые M раз (defer без счёта к DLQ), потом успех;
      ok    — успех сразу.
    Счётчик попыток на ключ потокобезопасен (несколько воркеров)."""

    def __init__(self, perm_pct, flaky_pct, bp_pct, flaky_fails, bp_defers, latency):
        self.perm_pct = perm_pct
        self.flaky_pct = flaky_pct
        self.bp_pct = bp_pct
        self.flaky_fails = flaky_fails
        self.bp_defers = bp_defers
        self.latency = latency
        self._attempts: dict[str, int] = collections.defaultdict(int)
        self._lock = threading.Lock()
        self.calls = 0

    def bucket(self, bkey: str) -> str:
        h = int(hashlib.blake2b(bkey.encode(), digest_size=8).hexdigest(), 16) % 10000
        perm = self.perm_pct * 100
        flaky = self.flaky_pct * 100
        bp = self.bp_pct * 100
        if h < perm:
            return "perm"
        if h < perm + flaky:
            return "flaky"
        if h < perm + flaky + bp:
            return "bp"
        return "ok"

    def __call__(self, event: Event) -> None:
        with self._lock:
            self.calls += 1
            self._attempts[event.business_key] += 1
            n = self._attempts[event.business_key]
        if self.latency:
            time.sleep(self.latency)
        b = self.bucket(event.business_key)
        if b == "perm":
            raise RuntimeError("simulated Z.AI hard failure")
        if b == "flaky" and n <= self.flaky_fails:
            raise RuntimeError("simulated transient Z.AI failure")
        if b == "bp" and n <= self.bp_defers:
            raise Backpressure("simulated local rate limit")
        # success → supervisor доведёт до DONE


class FakeClient:
    """Замена GitHub-публикатора. Считает коммент(ы) о провале (СТ-27)."""

    def __init__(self):
        self.comments = 0
        self._lock = threading.Lock()

    def upsert_comment(self, repo, number, marker, body):
        with self._lock:
            self.comments += 1


# ───────────────────────────── вспомогалки ─────────────────────────────
def sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def build_webhook(secret, repo, number, sha, delivery):
    payload = {
        "action": "opened",
        "pull_request": {"number": number, "head": {"sha": sha}},
        "repository": {"full_name": repo},
    }
    raw = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": sign(secret, raw),
    }
    return raw, headers


def percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# ───────────────────────────── Фаза A: нагрузка ─────────────────────────────
def phase_a(args, store_path, queue_path):
    secret = "loadtest-secret"
    webhooks = (args.events + COMMANDS_PER_WEBHOOK - 1) // COMMANDS_PER_WEBHOOK
    total_events = webhooks * COMMANDS_PER_WEBHOOK
    dup_every = max(1, int(1 / args.dup_rate)) if args.dup_rate else 0

    metrics.reset()
    analyze = FakeAnalyze(args.perm_fail_pct, args.flaky_pct, args.backpressure_pct,
                          args.flaky_fails, args.bp_defers, args.analyze_latency)
    client = FakeClient()
    producers_done = threading.Event()
    lock_retries = {"n": 0}
    lr_lock = threading.Lock()

    # ── продюсеры webhook'ов: каждый со своими соединениями (как процесс ingress) ──
    def produce(shard):
        st = StateStore(store_path)
        q = DurableQueue(queue_path)

        def schedule(event):
            q.enqueue(event_to_dict(event), event.repo)  # партиция = repo (СТ-7)

        for i in shard:
            # backpressure продюсера: держим бэклог ограниченным (реальный steady-state,
            # где темп приёма ≈ ёмкости воркеров), а не заваливаем очередь 100k разом.
            while args.max_depth and q.depth() > args.max_depth:
                time.sleep(0.01)
            repo = f"kibarik/loadtest-{i % args.repos:03d}"
            sha = f"{i:040d}"[-40:]
            raw, headers = build_webhook(secret, repo, i, sha, f"d-{i}")
            handle_webhook(raw, headers, secret=secret, store=st, schedule=schedule)
            if dup_every and i % dup_every == 0:  # СТ-2: повторная доставка — должна дедупнуться
                handle_webhook(raw, headers, secret=secret, store=st, schedule=schedule)

    # ── воркеры: каждый со своими соединениями (как процесс worker) ──
    def work():
        st = StateStore(store_path)
        q = DurableQueue(queue_path)
        idle = 0
        while True:
            try:
                progressed = run_once(
                    q, store=st, client=client, analyze=analyze,
                    visibility_timeout=args.vis_timeout, task_timeout=args.task_timeout,
                    max_attempts=args.max_attempts, backoff=args.backoff,
                    backpressure_delay=args.bp_delay)
            except sqlite3.OperationalError:
                # «database is locked» сверх busy_timeout под write-контеншеном: не
                # корректность, а сигнал нагрузки. Лизнутое сообщение передоставится
                # по visibility_timeout. Считаем и продолжаем (не прячем).
                with lr_lock:
                    lock_retries["n"] += 1
                progressed = True
            if progressed:
                idle = 0
                continue
            # лизать нечего прямо сейчас
            if producers_done.is_set() and q.depth() == 0:
                idle += 1
                if idle >= 3:
                    return
            time.sleep(0.003)

    t0 = time.time()
    prod_threads = []
    for w in range(args.producers):
        shard = range(w, webhooks, args.producers)
        th = threading.Thread(target=produce, args=(shard,), name=f"prod-{w}")
        th.start()
        prod_threads.append(th)
    work_threads = [threading.Thread(target=work, name=f"work-{w}") for w in range(args.workers)]
    for th in work_threads:
        th.start()

    for th in prod_threads:
        th.join()
    t_ingest = time.time()
    producers_done.set()
    for th in work_threads:
        th.join()
    t_drain = time.time()

    return _phase_a_report(args, store_path, queue_path, webhooks, total_events,
                           analyze, client, lock_retries["n"], t0, t_ingest, t_drain)


def _phase_a_report(args, store_path, queue_path, webhooks, total_events,
                    analyze, client, lock_retries, t0, t_ingest, t_drain):
    q = DurableQueue(queue_path)
    con = sqlite3.connect(store_path)
    con.row_factory = sqlite3.Row

    by_state = {r[0]: r[1] for r in con.execute(
        "SELECT state, COUNT(*) FROM events WHERE event_type!='reconcile' GROUP BY state").fetchall()}
    accepted = sum(by_state.values())
    done = by_state.get(State.DONE.value, 0)
    dead = by_state.get(State.DEAD_LETTER.value, 0)
    non_terminal = accepted - done - dead
    done_dupes = con.execute(
        "SELECT COUNT(*) FROM (SELECT business_key FROM events WHERE state='done' "
        "GROUP BY business_key HAVING COUNT(*)>1)").fetchone()[0]
    claims_left = con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    partitions = con.execute(
        "SELECT COUNT(DISTINCT repo) FROM events WHERE event_type!='reconcile'").fetchone()[0]

    lat = [r[0] for r in con.execute(
        "SELECT updated_at-created_at FROM events WHERE state IN ('done','dead_letter')").fetchall()]
    lat.sort()
    con.close()

    dlq_rows = len(q.dead_letters())
    depth = q.depth()

    ingest_s = t_ingest - t0
    drain_s = t_drain - t0
    m = metrics.snapshot()

    checks = []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    chk("К-1 нет тихих потерь (non-terminal==0)", non_terminal == 0, f"non_terminal={non_terminal}")
    chk("СТ-6/9 очередь осушена (depth==0)", depth == 0, f"depth={depth}")
    chk("СТ-16 идемпотентность (0 дублей DONE)", done_dupes == 0, f"done_dupes={done_dupes}")
    chk("СТ-27 dead_letter==dlq_rows==comments",
        dead == dlq_rows == client.comments,
        f"dead={dead} dlq_rows={dlq_rows} comments={client.comments}")
    chk("учёт done+dead==принято", done + dead == accepted, f"{done}+{dead} vs {accepted}")
    chk("СТ-2 dedup (принято==2×webhooks)", accepted == total_events,
        f"accepted={accepted} expected={total_events}")
    chk("захваты не текут (claims==0)", claims_left == 0, f"claims_left={claims_left}")
    chk("СТ-7 партиции обслужены", partitions == min(args.repos, webhooks),
        f"partitions={partitions}")

    print("\n" + "=" * 72)
    print("ФАЗА A — нагрузочный прогон конвейера")
    print("=" * 72)
    print(f"  webhooks:            {webhooks:,}  ×{COMMANDS_PER_WEBHOOK} команды = {total_events:,} событий")
    print(f"  воркеры/продюсеры:   {args.workers}/{args.producers}   репозитории(партиции): {args.repos}")
    print(f"  инъекции сбоев:      perm={args.perm_fail_pct}%  flaky={args.flaky_pct}%  backpressure={args.backpressure_pct}%")
    print("-" * 72)
    print(f"  принято событий:     {accepted:,}")
    print(f"  → DONE:              {done:,}")
    print(f"  → DEAD_LETTER:       {dead:,}   (видимых комментов: {client.comments:,})")
    print(f"  вызовов analyze:     {analyze.calls:,}  (>события из-за ретраев flaky/bp)")
    print(f"  метрики: processed_ok={m.get('processed_ok',0):,} dead_letter_total={m.get('dead_letter_total',0):,} "
          f"backpressure_deferred={m.get('backpressure_deferred',0):,}")
    print(f"  lock-retries (SQLite): {lock_retries:,}")
    print("-" * 72)
    print(f"  приём (ingest):      {ingest_s:6.2f}s  →  {total_events/ingest_s:,.0f} соб/с")
    print(f"  полная обработка:    {drain_s:6.2f}s  →  {total_events/drain_s:,.0f} соб/с (end-to-end)")
    print(f"  латентность событие→терминал: p50={percentile(lat,0.5)*1000:.0f}ms "
          f"p95={percentile(lat,0.95)*1000:.0f}ms p99={percentile(lat,0.99)*1000:.0f}ms "
          f"max={ (lat[-1]*1000 if lat else 0):.0f}ms")
    print("-" * 72)
    all_ok = True
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if not ok or detail else ""))
        all_ok = all_ok and ok
    print("=" * 72)

    return {
        "ok": all_ok, "webhooks": webhooks, "total_events": total_events,
        "accepted": accepted, "done": done, "dead": dead, "non_terminal": non_terminal,
        "dlq_rows": dlq_rows, "comments": client.comments, "analyze_calls": analyze.calls,
        "lock_retries": lock_retries, "depth": depth, "claims_left": claims_left,
        "ingest_s": ingest_s, "drain_s": drain_s,
        "throughput_ingest": total_events / ingest_s, "throughput_e2e": total_events / drain_s,
        "p50_ms": percentile(lat, 0.5) * 1000, "p95_ms": percentile(lat, 0.95) * 1000,
        "p99_ms": percentile(lat, 0.99) * 1000, "max_ms": (lat[-1] * 1000 if lat else 0),
        "metrics": m, "checks": checks,
    }


# ───────────────────── Фаза B: reconciliation sweeper ─────────────────────
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def phase_b():
    """Свипер как бэкстоп: пропущенный webhook, застрявший воркер, отравленное
    событие — все доводятся до корректного терминала (СТ-13/29/32)."""
    clk = Clock()
    store = StateStore(":memory:", clock=clk)
    queue = DurableQueue(":memory:", clock=clk)
    client = FakeClient()

    # 1) застрявшее событие (воркер умер в PROCESSING), attempts<max → должно ре-queue → DONE
    stuck = Event("stuck-1", "kibarik/lt", 1, "aaa", "/review")
    store.record_received(stuck)
    for s in (State.QUEUED, State.PROCESSING):
        store.transition("stuck-1", s)

    # 2) отравленное застрявшее (attempts>=max) → должно уйти в DEAD_LETTER + коммент
    poison = Event("poison-1", "kibarik/lt", 2, "bbb", "/review")
    store.record_received(poison)
    store.transition("poison-1", State.QUEUED)
    store.transition("poison-1", State.PROCESSING)
    for _ in range(5):
        store.increment_attempt("poison-1")

    # 3) пропущенный webhook: PR открыт, но события в сторе нет вовсе → reconcile-enqueue
    missed_pr = OpenPR("kibarik/lt", 3, "ccc")

    clk.t += 10_000  # состарить всё за stale_deadline

    def enqueue(ev, force=False):
        queue.enqueue(event_to_dict(ev), ev.repo)

    rep = sweep(
        store, list_open_prs=lambda: [missed_pr],
        has_completed_review=make_has_completed_review(store),
        enqueue=enqueue, client=client, commands=["/review"],
        stale_deadline=1.0, max_attempts=5, max_cycles=3)

    # осушаем очередь одним воркером (analyze всегда успешен)
    ok_analyze = FakeAnalyze(0, 0, 0, 0, 0, 0)
    drained = 0
    while run_once(queue, store=store, client=client, analyze=ok_analyze,
                   visibility_timeout=30, task_timeout=10, max_attempts=5):
        drained += 1

    stuck_state = store.state_of("stuck-1")
    poison_state = store.state_of("poison-1")
    # reconcile для missed_pr создаёт событие с делверей reconcile:...:seq — ищем по бизнес-ключу
    missed_done = store._db.execute(  # noqa: SLF001 — read-only проверка в тесте
        "SELECT COUNT(*) FROM events WHERE repo=? AND number=? AND state='done'",
        ("kibarik/lt", 3)).fetchone()[0]

    checks = [
        ("СТ-13 застрявшее → восстановлено (DONE)", stuck_state == State.DONE, str(stuck_state)),
        ("СТ-13 отравленное → DEAD_LETTER", poison_state == State.DEAD_LETTER, str(poison_state)),
        ("СТ-27 dead-letter дал коммент", client.comments >= 1, f"comments={client.comments}"),
        ("СТ-29 пропущенный PR → reconcile → DONE", missed_done >= 1, f"done={missed_done}"),
    ]

    print("\n" + "=" * 72)
    print("ФАЗА B — reconciliation sweeper (бэкстоп)")
    print("=" * 72)
    print(f"  sweep: requeued={len(rep.requeued)} dead_lettered={len(rep.dead_lettered)} "
          f"reconciled={len(rep.reconciled)}  затем осушено воркером={drained}")
    all_ok = True
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
        all_ok = all_ok and ok
    print("=" * 72)
    return {"ok": all_ok, "checks": checks, "report": {
        "requeued": len(rep.requeued), "dead_lettered": len(rep.dead_lettered),
        "reconciled": len(rep.reconciled), "drained": drained}}


# ───────────────────────────── отчёт в файл ─────────────────────────────
def write_results(path, a, b, args):
    def rows(checks):
        return "\n".join(f"| {'✅' if ok else '❌'} | {n} | `{d}` |" for n, ok, d in checks)

    md = f"""# Нагрузочный прогон ядра надёжности — результаты

Синтетический прогон **настоящего** конвейера `ingress → durable queue → worker → sweeper`
(замоканы только Z.AI и GitHub). Топология как в проде на одном узле: общий SQLite на диске,
{args.workers} воркеров, {args.producers} продюсеров, {args.repos} репозиториев-партиций.

## Итог: {"✅ ВСЕ ИНВАРИАНТЫ ВЫПОЛНЕНЫ" if a['ok'] and b['ok'] else "❌ ЕСТЬ НАРУШЕНИЯ"}

## Фаза A — нагрузка ({a['total_events']:,} событий)

| Метрика | Значение |
|---|---|
| Webhooks → события | {a['webhooks']:,} → {a['total_events']:,} |
| Принято (после dedup) | {a['accepted']:,} |
| DONE | {a['done']:,} |
| DEAD_LETTER (= комментов) | {a['dead']:,} (= {a['comments']:,}) |
| Вызовов analyze (с ретраями) | {a['analyze_calls']:,} |
| SQLite lock-retries | {a['lock_retries']:,} |
| Throughput (ingest) | {a['throughput_ingest']:,.0f} соб/с |
| Throughput (end-to-end) | {a['throughput_e2e']:,.0f} соб/с |
| Латентность p50 / p95 / p99 / max | {a['p50_ms']:.0f} / {a['p95_ms']:.0f} / {a['p99_ms']:.0f} / {a['max_ms']:.0f} ms |

| ✔ | Инвариант | Детали |
|---|---|---|
{rows(a['checks'])}

## Фаза B — reconciliation sweeper

sweep: requeued={b['report']['requeued']}, dead_lettered={b['report']['dead_lettered']}, reconciled={b['report']['reconciled']}, затем осушено={b['report']['drained']}

| ✔ | Инвариант | Детали |
|---|---|---|
{rows(b['checks'])}

---
_Прогон детерминирован (инъекция сбоев по хешу бизнес-ключа); повторяемо через
`python3 reliability/loadtest/run_loadtest.py --events {args.events}`._
"""
    with open(path, "w") as f:
        f.write(md)


def main():
    p = argparse.ArgumentParser(description="Нагрузочный прогон ядра надёжности")
    p.add_argument("--events", type=int, default=100500, help="целевое число событий")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--producers", type=int, default=4)
    p.add_argument("--repos", type=int, default=64, help="число репозиториев-партиций")
    p.add_argument("--perm-fail-pct", type=float, default=2.0, help="%% ключей с перманентным сбоем → DLQ")
    p.add_argument("--flaky-pct", type=float, default=6.0, help="%% flaky (падают, потом успех)")
    p.add_argument("--backpressure-pct", type=float, default=2.0, help="%% backpressure (defer, потом успех)")
    p.add_argument("--flaky-fails", type=int, default=2)
    p.add_argument("--bp-defers", type=int, default=2)
    p.add_argument("--max-attempts", type=int, default=8)
    p.add_argument("--backoff", type=float, default=0.02)
    p.add_argument("--vis-timeout", type=float, default=30.0)
    p.add_argument("--task-timeout", type=float, default=10.0)
    p.add_argument("--bp-delay", type=float, default=0.05)
    p.add_argument("--dup-rate", type=float, default=0.05, help="доля webhook'ов с повторной доставкой")
    p.add_argument("--max-depth", type=int, default=3000, help="потолок глубины очереди (backpressure продюсеров; 0=без)")
    p.add_argument("--analyze-latency", type=float, default=0.0, help="искусств. задержка analyze, с")
    p.add_argument("--db-dir", default="", help="каталог для SQLite (по умолчанию временный)")
    p.add_argument("--keep-db", action="store_true")
    p.add_argument("--out", default="", help="путь к markdown-отчёту")
    p.add_argument("--skip-phase-b", action="store_true")
    args = p.parse_args()

    import tempfile
    db_dir = args.db_dir or tempfile.mkdtemp(prefix="reliability-loadtest-")
    os.makedirs(db_dir, exist_ok=True)
    store_path = os.path.join(db_dir, "reliability.db")
    queue_path = os.path.join(db_dir, "queue.db")
    print(f"SQLite: {db_dir}")

    a = phase_a(args, store_path, queue_path)
    b = phase_b() if not args.skip_phase_b else {"ok": True, "checks": [], "report":
                                                 {"requeued": 0, "dead_lettered": 0, "reconciled": 0, "drained": 0}}

    if args.out:
        write_results(args.out, a, b, args)
        print(f"\nОтчёт: {args.out}")

    if not args.keep_db:
        import shutil
        shutil.rmtree(db_dir, ignore_errors=True)

    ok = a["ok"] and b["ok"]
    print(f"\n{'✅ PASS — система держит нагрузку без потерь' if ok else '❌ FAIL — есть нарушения инвариантов'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
