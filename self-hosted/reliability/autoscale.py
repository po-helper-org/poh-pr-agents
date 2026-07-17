"""Политика автоскейла воркеров (СТ-18) — чистая функция, без побочных эффектов.

Само масштабирование выполняет оркестратор (Dokploy/compose scale/k8s HPA) — здесь
только ПОЛИТИКА: сколько воркеров нужно под текущую глубину/возраст очереди. Вынос
в тестируемую функцию: политику проверяем без инфраструктуры, а раннер (deploy)
лишь опрашивает `/metrics` и применяет число. Предпосылка 100k/сутки — очередь не
должна расти неограниченно, но и не жечь ресурсы на холостом ходу.
"""
from __future__ import annotations


def desired_workers(depth: int, oldest_age_s: float, *, per_worker: int = 20,
                    min_workers: int = 1, max_workers: int = 20,
                    age_pressure_s: float = 300.0) -> int:
    """Желаемое число воркеров.

    - База: глубина/`per_worker` (сколько задач тянет один воркер до целевого лага).
    - Возрастное давление: если самая старая задача ждёт дольше `age_pressure_s`,
      добавляем воркеров пропорционально «просрочке» — иначе p95 (К-3) уплывёт даже
      при небольшой глубине (застряла узкая партиция).
    - Зажим в [min_workers, max_workers]: max — потолок против перегрузки GitHub/LLM
      rate-limit (СТ-24), min — всегда есть кому разгребать.
    """
    if depth <= 0:
        return min_workers
    base = -(-depth // max(1, per_worker))  # ceil division
    age_extra = int(oldest_age_s // age_pressure_s) if oldest_age_s > age_pressure_s else 0
    return max(min_workers, min(max_workers, base + age_extra))
