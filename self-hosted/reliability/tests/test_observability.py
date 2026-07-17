"""СТ-18, 33..35: экспозиция метрик (Prometheus) + политика автоскейла."""
import unittest

from reliability import metrics
from reliability.autoscale import desired_workers


class TestRenderPrometheus(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def test_renders_counters_with_type_and_prefix(self):
        metrics.incr("dead_letter_total", 2)
        metrics.incr("gateway_success", 5)
        out = metrics.render_prometheus()
        self.assertIn("# TYPE reliability_dead_letter_total counter", out)
        self.assertIn("reliability_dead_letter_total 2", out)
        self.assertIn("reliability_gateway_success 5", out)
        self.assertTrue(out.endswith("\n"))

    def test_renders_gauges(self):
        out = metrics.render_prometheus({"queue_depth": 7, "dead_letters": 1})
        self.assertIn("# TYPE reliability_queue_depth gauge", out)
        self.assertIn("reliability_queue_depth 7", out)
        self.assertIn("reliability_dead_letters 1", out)

    def test_empty_is_valid(self):
        self.assertEqual(metrics.render_prometheus(), "\n")


class TestDesiredWorkers(unittest.TestCase):
    def test_idle_returns_min(self):
        self.assertEqual(desired_workers(0, 0.0, min_workers=2), 2)

    def test_scales_with_depth(self):
        self.assertEqual(desired_workers(50, 0.0, per_worker=20), 3)  # ceil(50/20)

    def test_age_pressure_adds_workers(self):
        # мелкая глубина, но задача застряла надолго → добавляем воркеров
        n = desired_workers(5, 650.0, per_worker=20, age_pressure_s=300)
        self.assertEqual(n, 1 + 2)  # base ceil(5/20)=1 + floor(650/300)=2

    def test_capped_at_max(self):
        self.assertEqual(desired_workers(100000, 0.0, per_worker=20, max_workers=16), 16)

    def test_floor_at_min(self):
        self.assertEqual(desired_workers(1, 0.0, per_worker=20, min_workers=3), 3)


if __name__ == "__main__":
    unittest.main()
