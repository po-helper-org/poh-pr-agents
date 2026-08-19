"""ФТ-APRP-10 / СТ-24: тиринг моделей — маршрутизация к cheap/deep, fallback, конфиг."""
import unittest

from reliability import metrics
from reliability.gateway import TieredGateway, build_tiered_gateway
from reliability.state import Event


def passthrough(fn, timeout):
    return fn()


def make_event():
    return Event("d1", "o/r", 7, "abc", "/review")


class TestTieredGateway(unittest.TestCase):
    def setUp(self):
        metrics.reset()
        self.hits = []
        specs = [
            {"tier": "deep", "name": "glm-5", "invoke": lambda e: self.hits.append("deep"),
             "attempt_timeout": 200},
            {"tier": "cheap", "name": "glm-4.7", "invoke": lambda e: self.hits.append("cheap"),
             "attempt_timeout": 40, "rate": 5},
        ]
        self.tg = build_tiered_gateway(specs, run_fn=passthrough)

    def test_routes_to_deep(self):
        self.tg.run(make_event(), tier="deep")
        self.assertEqual(self.hits, ["deep"])

    def test_routes_to_cheap(self):
        self.tg.run(make_event(), tier="cheap")
        self.assertEqual(self.hits, ["cheap"])

    def test_default_tier_is_deep(self):
        self.tg.run(make_event())
        self.assertEqual(self.hits, ["deep"])

    def test_unknown_tier_falls_back_to_deep(self):
        self.tg.run(make_event(), tier="ultra")
        self.assertEqual(self.hits, ["deep"])
        self.assertEqual(metrics.get("gateway_tier_fallback_deep"), 1)

    def test_tiers_listed(self):
        self.assertEqual(sorted(self.tg.tiers()), ["cheap", "deep"])

    def test_deep_required(self):
        with self.assertRaises(ValueError):
            TieredGateway({"cheap": object()})


if __name__ == "__main__":
    unittest.main()
