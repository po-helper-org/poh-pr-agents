"""Периодический раннер reconciliation sweeper (deploy entrypoint).

Отдельный процесс: раз в `RELIABILITY_SWEEP_INTERVAL` секунд сверяет открытые PR
со state store и дозапускает пропущенное/застрявшее. Делит SQLite (state+queue)
с ingress/worker. Целиком под pragma — проверяется на живом смоуке.
"""
from __future__ import annotations


def main():  # pragma: no cover - deploy entrypoint
    import os
    import time

    from reliability import analyze_adapter, logging_setup
    logging_setup.configure()  # reliability.* → stdout (логи свипера в контейнере sweeper)
    from reliability.github_client import GitHubAppClient
    from reliability.queue import DurableQueue
    from reliability.state import StateStore, event_to_dict
    from reliability.sweeper import sweep
    from reliability.sweeper_adapter import (
        make_github_review_verifier,
        make_has_completed_review,
        make_list_open_prs,
        make_list_open_prs_all,
    )

    store = StateStore(os.environ.get("RELIABILITY_DB", "/data/reliability.db"))
    queue = DurableQueue(os.environ.get("RELIABILITY_QUEUE", "/data/queue.db"))
    client = GitHubAppClient(token_provider=analyze_adapter.installation_token)
    repos = [r.strip() for r in os.environ.get("RELIABILITY_REPOS", "").split(",") if r.strip()]
    commands = [c.strip() for c in os.environ.get("RELIABILITY_COMMANDS", "/describe,/review").split(",") if c.strip()]
    interval = int(os.environ.get("RELIABILITY_SWEEP_INTERVAL", "300"))
    stale_deadline = int(os.environ.get("RELIABILITY_STALE_DEADLINE", "1800"))
    max_attempts = int(os.environ.get("RELIABILITY_MAX_ATTEMPTS", "5"))
    max_cycles = int(os.environ.get("RELIABILITY_SWEEP_MAX_CYCLES", "6"))

    def enqueue(event, *, force=False):
        queue.enqueue(event_to_dict(event), event.repo)  # force выводится воркером из event_type

    # store-only по умолчанию; RELIABILITY_VERIFY_GITHUB=1 включает детект
    # проглоченного сбоя (сверка артефакта на GitHub) — эвристика тюнится на смоуке.
    verify = (make_github_review_verifier(client)
              if os.environ.get("RELIABILITY_VERIFY_GITHUB") == "1" else None)
    has_completed_review = make_has_completed_review(store, verify=verify)

    import logging
    log = logging.getLogger("reliability.sweeper")
    # RELIABILITY_REPOS задан → сверяем ровно эти репо; пуст → org-wide: App сам
    # определяет охват через свои установки (все репо всех орг/аккаунтов, где он
    # установлен, включая новые). Живые PR-события и так идут по webhook для любого
    # установленного репо — это лишь бэкстоп на пропущенные webhook'и.
    if repos:
        list_open_prs = make_list_open_prs(client, repos)
        log.info("sweeper started: repos=%s commands=%s interval=%ss", repos, commands, interval)
    else:
        list_open_prs = make_list_open_prs_all(client, analyze_adapter.provider())
        log.info("sweeper started: ORG-WIDE (RELIABILITY_REPOS пуст — обход всех установок App) "
                 "commands=%s interval=%ss", commands, interval)
    while True:
        rep = sweep(store, list_open_prs=list_open_prs, has_completed_review=has_completed_review,
                    enqueue=enqueue, client=client, commands=commands,
                    stale_deadline=stale_deadline, max_attempts=max_attempts, max_cycles=max_cycles)
        # тихо, если делать нечего; строка — только когда реально что-то сделали
        if rep.reconciled or rep.requeued or rep.dead_lettered or rep.escalated:
            log.info("sweep: reconciled=%d requeued=%d dead_lettered=%d escalated=%d",
                     len(rep.reconciled), len(rep.requeued),
                     len(rep.dead_lettered), len(rep.escalated))
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    main()
