"""СТ-19..24: LLM Gateway — circuit breaker, rate limit, failover, таймаут."""
import unittest

from reliability import metrics
from reliability.gateway import (
    Circuit,
    CircuitBreaker,
    Gateway,
    GatewayUnavailable,
    Provider,
    RateLimited,
    TokenBucket,
)
from reliability.state import Backpressure, Event


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def ev():
    return Event("d1", "o/r", 7, "abc", "/review")


def passthrough(fn, timeout):
    return fn()


def raising(exc):
    def _run(fn, timeout):
        raise exc
    return _run


class TestCircuitBreaker(unittest.TestCase):
    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=10, clock=_Clock())
        for _ in range(3):
            self.assertTrue(cb.allow())
            cb.record_failure()
        self.assertFalse(cb.allow())          # разомкнута
        self.assertEqual(cb.state, Circuit.OPEN)

    def test_success_resets_failures(self):
        cb = CircuitBreaker(failure_threshold=3, clock=_Clock())
        cb.record_failure()
        cb.record_failure()
        cb.record_success()                   # сброс
        cb.record_failure()
        cb.record_failure()
        self.assertTrue(cb.allow())           # ещё не 3 подряд

    def test_half_open_after_cooldown_then_close_on_success(self):
        clk = _Clock()
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=10, clock=clk)
        cb.record_failure()                   # OPEN
        self.assertFalse(cb.allow())
        clk.t = 10                            # остыло
        self.assertEqual(cb.state, Circuit.HALF_OPEN)
        self.assertTrue(cb.allow())           # пробный вызов пропускаем
        cb.record_success()
        self.assertEqual(cb.state, Circuit.CLOSED)

    def test_half_open_failure_reopens(self):
        clk = _Clock()
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=10, clock=clk)
        cb.record_failure()                   # OPEN
        clk.t = 10
        self.assertEqual(cb.state, Circuit.HALF_OPEN)
        cb.record_failure()                   # проба сбойна → снова OPEN
        self.assertEqual(cb.state, Circuit.OPEN)
        self.assertFalse(cb.allow())


class TestTokenBucket(unittest.TestCase):
    def test_capacity_then_refill(self):
        clk = _Clock()
        tb = TokenBucket(rate=1.0, capacity=2, clock=clk)
        self.assertTrue(tb.try_acquire())     # 2 -> 1
        self.assertTrue(tb.try_acquire())     # 1 -> 0
        self.assertFalse(tb.try_acquire())    # пусто
        clk.t = 1.0                           # +1 токен
        self.assertTrue(tb.try_acquire())
        self.assertFalse(tb.try_acquire())

    def test_refill_capped_at_capacity(self):
        clk = _Clock()
        tb = TokenBucket(rate=100.0, capacity=3, clock=clk)
        clk.t = 100                           # прошло много — но не больше ёмкости
        for _ in range(3):
            self.assertTrue(tb.try_acquire())
        self.assertFalse(tb.try_acquire())


class Spy:
    def __init__(self, exc=None):
        self.exc, self.calls = exc, 0

    def __call__(self, event):
        self.calls += 1
        if self.exc:
            raise self.exc


class TestGateway(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def test_success_passthrough(self):
        spy = Spy()
        g = Gateway([Provider("z", spy)], run_fn=passthrough)
        g.run(ev())
        self.assertEqual(spy.calls, 1)
        self.assertEqual(metrics.get("gateway_success"), 1)

    def test_failover_to_second_provider(self):
        bad, good = Spy(exc=RuntimeError("z down")), Spy()
        g = Gateway([Provider("z", bad), Provider("backup", good)], run_fn=passthrough)
        g.run(ev())
        self.assertEqual(bad.calls, 1)
        self.assertEqual(good.calls, 1)       # переключились
        self.assertEqual(metrics.get("gateway_failover"), 1)

    def test_all_providers_fail_raises_unavailable(self):
        g = Gateway([Provider("z", Spy(exc=RuntimeError("down")))], run_fn=passthrough)
        with self.assertRaises(GatewayUnavailable):
            g.run(ev())
        self.assertEqual(metrics.get("gateway_unavailable"), 1)

    def test_circuit_opens_then_fails_fast(self):
        spy = Spy(exc=RuntimeError("down"))
        cb = CircuitBreaker(failure_threshold=2, reset_timeout=999, clock=_Clock())
        g = Gateway([Provider("z", spy, breaker=cb)], run_fn=passthrough)
        for _ in range(2):
            with self.assertRaises(GatewayUnavailable):
                g.run(ev())
        calls_before = spy.calls
        # цепь разомкнута → следующий вызов отказывает МГНОВЕННО, провайдера не трогаем
        with self.assertRaises(GatewayUnavailable):
            g.run(ev())
        self.assertEqual(spy.calls, calls_before)     # быстрый отказ, без вызова
        self.assertEqual(metrics.get("gateway_circuit_open"), 1)

    def test_timeout_counts_as_failure(self):
        spy = Spy()
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=999, clock=_Clock())
        g = Gateway([Provider("z", spy, breaker=cb)], run_fn=raising(TimeoutError("stall")))
        with self.assertRaises(GatewayUnavailable):
            g.run(ev())
        self.assertFalse(cb.allow())          # таймаут разомкнул цепь
        self.assertEqual(metrics.get("gateway_provider_failure"), 1)

    def test_rate_limit_raises_backpressure(self):
        clk = _Clock()
        tb = TokenBucket(rate=0.0, capacity=1, clock=clk)
        spy = Spy()
        g = Gateway([Provider("z", spy)], limiter=tb, run_fn=passthrough)
        g.run(ev())                           # 1-й токен ок
        with self.assertRaises(RateLimited):
            g.run(ev())                       # токенов нет → backpressure
        self.assertEqual(spy.calls, 1)
        self.assertEqual(metrics.get("gateway_rate_limited"), 1)

    def test_open_circuit_does_not_consume_token(self):
        # провайдер с разомкнутой цепью не должен жечь токены лимитера
        clk = _Clock()
        tb = TokenBucket(rate=0.0, capacity=1, clock=clk)
        good = Spy()
        cb_open = CircuitBreaker(failure_threshold=1, reset_timeout=999, clock=_Clock())
        cb_open.record_failure()              # разомкнута
        g = Gateway([Provider("dead", Spy(), breaker=cb_open), Provider("z", good)],
                    limiter=tb, run_fn=passthrough)
        g.run(ev())
        self.assertEqual(good.calls, 1)       # дошли до живого, токен потратили на него
        self.assertEqual(metrics.get("gateway_success"), 1)

    def test_requires_at_least_one_provider(self):
        with self.assertRaises(ValueError):
            Gateway([], run_fn=passthrough)

    def test_rate_limited_is_backpressure(self):
        # воркер ловит базовый Backpressure, не завися от gateway → откладывает,
        # а не метит как сбой (иначе троттлинг = ложный DLQ)
        self.assertTrue(issubclass(RateLimited, Backpressure))


if __name__ == "__main__":
    unittest.main()
