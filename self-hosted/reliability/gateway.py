"""LLM Gateway (СТ-19..24): единая защищённая точка вызова анализа/LLM.

Стоит между воркером и pr-agent (который ходит в Z.AI). Даёт:
- **circuit breaker на провайдера** (СТ-22): после N подряд сбоев/таймаутов цепь
  размыкается → последующие вызовы отказывают МГНОВЕННО (GatewayCircuitOpen), не
  вися по таймауту. Это backpressure: воркер ОТКЛАДЫВАЕТ PR до восстановления
  провайдера, а не дедлетерит его провал-комментом (системный простой ≠ дефект PR;
  иначе один аутейдж Z.AI спамит провалами весь org-wide бэклог). К-3 (не жжём
  worker-секунды на мёртвый Z.AI) сохраняется; «не молчать» на затяжной простой —
  эскалация свипера после max_cycles.
- **token-bucket rate limit** (СТ-20): держим поток запросов под лимитом провайдера;
  переполнение → backpressure (`RateLimited`), очередь разложит во времени.
- **failover по пулу провайдеров** (СТ-19/21): пробуем следующий на сбое. Один ключ
  Z.AI = вырожденный пул из одного провайдера, но seam для добавления ключей готов.
- **таймаут на попытку** (СТ-14): зависший вызов = сбой цепи, а не вечное ожидание.

Разделение слоёв ретрая (важно, чтобы не было двойного ретрая): gateway делает
failover ВШИРЬ (по провайдерам за один вызов), очередь+воркер — ретрай ВГЛУБЬ
(во времени, backoff/DLQ). Часы/сон/таймаут инъектируются → тесты без реального
времени и потоков.

⚠️ Состояние breaker/limiter — ПРОЦЕССНОЕ (in-memory). При N репликах воркера у
каждой свой bucket → эффективный RPS ≈ N×rate: на масштабе задавать rate = лимит
Z.AI / N (или вынести общий limiter в Redis за тем же интерфейсом, как и очередь).
Breaker тоже per-process — «быстрый отказ» срабатывает независимо на каждой реплике;
для одного узла Dokploy (где и SQLite-очередь одноузловая) это приемлемо.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Optional

from reliability import metrics, sentry_setup
from reliability.state import Backpressure, Event

Invoke = Callable[[Event], None]  # реальный вызов провайдера; бросает при сбое


class GatewayUnavailable(Exception):
    """Провайдеров РЕАЛЬНО звали (≥1 попытка за этот вызов), но все сбоили. Воркер
    → nack (ретрай/DLQ+коммент): это «не молчать» для настоящего сбоя попытки."""


class GatewayCircuitOpen(Backpressure):
    """Все цепи провайдеров разомкнуты — звонков в этот раз НЕ было (системный
    простой, уже подтверждённый circuit breaker'ом). Backpressure: воркер ОТЛОЖИТ
    без счёта к DLQ и без провал-коммента — PR ни при чём. Держим до восстановления
    провайдера; затяжной простой закрывает эскалация свипера (max_cycles), а не спам
    провалов на весь бэклог при первом же аутейдже Z.AI."""


class RateLimited(Backpressure):
    """Превышен локальный лимит запросов — backpressure: воркер отложит без счёта
    к DLQ и без ложного коммента о провале (не сбой, а сдерживание потока)."""


class Circuit(str, enum.Enum):
    CLOSED = "closed"        # норма: пропускаем
    OPEN = "open"            # разомкнута: отказываем мгновенно
    HALF_OPEN = "half_open"  # проба после остывания: один вызов на разведку


class CircuitBreaker:
    """Размыкается после `failure_threshold` подряд сбоев; через `reset_timeout`
    секунд переходит в HALF_OPEN (пропустит один пробный вызов). Успех в HALF_OPEN
    замыкает цепь, сбой — снова размыкает."""

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0,
                 clock: Callable[[], float] = None):
        import time
        self._threshold = failure_threshold
        self._reset = reset_timeout
        self._clock = clock or time.monotonic
        self._failures = 0
        self._state = Circuit.CLOSED
        self._opened_at = 0.0

    @property
    def state(self) -> Circuit:
        # ленивый переход OPEN → HALF_OPEN по истечении остывания
        if self._state == Circuit.OPEN and self._clock() - self._opened_at >= self._reset:
            self._state = Circuit.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        return self.state != Circuit.OPEN  # CLOSED и HALF_OPEN пропускают

    def record_success(self) -> None:
        self._failures = 0
        self._state = Circuit.CLOSED

    def record_failure(self) -> None:
        # сбой пробы в HALF_OPEN → сразу назад в OPEN (не ждём порога)
        if self.state == Circuit.HALF_OPEN:
            self._state = Circuit.OPEN
            self._opened_at = self._clock()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = Circuit.OPEN
            self._opened_at = self._clock()


class TokenBucket:
    """Классический token-bucket: `rate` токенов/сек, ёмкость `capacity`.
    `try_acquire()` неблокирующий — вернёт False, если токенов нет (backpressure)."""

    def __init__(self, rate: float, capacity: float,
                 clock: Callable[[], float] = None):
        import time
        self._rate = rate
        self._capacity = capacity
        self._clock = clock or time.monotonic
        self._tokens = capacity
        self._last = self._clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def try_acquire(self, cost: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False


@dataclass
class Provider:
    name: str
    invoke: Invoke
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)


# Механизм таймаута попытки инъектируется (как в worker) → тесты без потоков.
def _default_run_fn(fn: Callable, timeout: float):  # pragma: no cover - реальные потоки
    from reliability.worker import run_with_timeout
    return run_with_timeout(fn, timeout)


class Gateway:
    def __init__(self, providers: list[Provider], *, limiter: Optional[TokenBucket] = None,
                 attempt_timeout: float = 75.0, run_fn: Callable = _default_run_fn):
        if not providers:
            raise ValueError("gateway requires at least one provider")
        self._providers = providers
        self._limiter = limiter
        self._attempt_timeout = attempt_timeout
        self._run_fn = run_fn

    def run(self, event: Event) -> None:
        """analyze-совместимый вход: провести анализ через защищённый пул.
        Успех — молча (как analyze). RateLimited — backpressure (воркер отложит).
        Если провайдера РЕАЛЬНО звали и он сбоил → GatewayUnavailable (воркер → nack:
        ретрай/DLQ+коммент, «не молчать» для сбоя попытки). Если же все цепи разомкнуты
        (системный простой, звонков не было) → GatewayCircuitOpen (тоже backpressure):
        держим PR отложенным до восстановления провайдера, а не дедлетерим ложным
        провал-комментом (иначе один аутейдж Z.AI спамит провалами весь org-wide
        бэклог). «Не молчать» на затяжной простой — эскалация свипера после max_cycles."""
        errors: list[tuple[str, str]] = []
        attempted = False  # был ли РЕАЛЬНЫЙ вызов провайдера (иначе всё — открытые цепи)
        for p in self._providers:
            if not p.breaker.allow():
                metrics.incr("gateway_circuit_open")
                errors.append((p.name, "circuit_open"))
                continue
            # токен берём только когда реально собираемся звонить провайдеру
            if self._limiter is not None and not self._limiter.try_acquire():
                metrics.incr("gateway_rate_limited")
                raise RateLimited("local rate limit exceeded")
            attempted = True
            try:
                self._run_fn(lambda: p.invoke(event), self._attempt_timeout)
            except Exception as e:  # таймаут или сбой провайдера — это сбой цепи
                p.breaker.record_failure()
                metrics.incr("gateway_provider_failure")
                errors.append((p.name, type(e).__name__))
                continue
            p.breaker.record_success()
            metrics.incr("gateway_success")
            if errors:  # дошли не с первого провайдера
                metrics.incr("gateway_failover")
            return
        if not attempted:
            # ни одного вызова: все цепи разомкнуты. Breaker уже подтвердил системный
            # простой провайдера — это НЕ дефект PR. Backpressure → воркер отложит без
            # счёта к DLQ и без провал-коммента; иначе org-wide бэклог самоуничтожается
            # в дедлетеры при первом же аутейдже Z.AI. Восстановится провайдер — добьём.
            metrics.incr("gateway_circuit_deferred")
            raise GatewayCircuitOpen(f"all circuits open: {errors}")
        metrics.incr("gateway_unavailable")
        # Реальный сбой попытки (провайдеров звали, все сбоили) — эскалация в Sentry.
        # Системный простой (GatewayCircuitOpen, звонков не было) сюда НЕ доходит и в
        # Sentry не идёт: аутейдж Z.AI не должен спамить issue на каждый PR бэклога.
        sentry_setup.capture_gateway_unavailable(event, errors)
        raise GatewayUnavailable(f"all providers failed: {errors}")
