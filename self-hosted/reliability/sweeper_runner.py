"""Периодический раннер reconciliation sweeper (deploy entrypoint).

Отдельный процесс: раз в `RELIABILITY_SWEEP_INTERVAL` секунд сверяет открытые PR
со state store и дозапускает пропущенное/застрявшее. Делит SQLite (state+queue)
с ingress/worker. Целиком под pragma — проверяется на живом смоуке.
"""
from __future__ import annotations


def main():  # pragma: no cover - deploy entrypoint
    import os
    import time

    from reliability import analyze_adapter
    from reliability.github_client import GitHubAppClient
    from reliability.queue import DurableQueue
    from reliability.state import StateStore, event_to_dict
    from reliability.sweeper import sweep
    from reliability.sweeper_adapter import (
        make_github_review_verifier,
        make_has_completed_review,
        make_list_open_prs,
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
    list_open_prs = make_list_open_prs(client, repos)

    while True:
        sweep(store, list_open_prs=list_open_prs, has_completed_review=has_completed_review,
              enqueue=enqueue, client=client, commands=commands,
              stale_deadline=stale_deadline, max_attempts=max_attempts, max_cycles=max_cycles)
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    main()
