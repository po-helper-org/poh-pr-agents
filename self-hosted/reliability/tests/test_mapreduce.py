"""ФТ-APRP-2/6/8: оркестрация — маршрутизация, fan-out payloads, fan-in claim, сбор."""
import unittest

from reliability.mapreduce import (
    CHUNK_EVENT,
    build_chunk_payloads,
    claim_reduce,
    collect_results,
    job_key_for,
    route,
)
from reliability.sizing import FileChange, SizeClass
from reliability.state import StateStore


class TestRoute(unittest.TestCase):
    def test_small_no_plan(self):
        files = [FileChange("a.py", 10, 0)]  # ~160 токенов
        sc, w, plan = route(files, chunk_budget_tokens=10_000)
        self.assertEqual(sc, SizeClass.SMALL)
        self.assertIsNone(plan)

    def test_large_builds_plan(self):
        files = [FileChange(f"f{i}.py", 200, 0) for i in range(10)]  # ~24k токенов
        sc, w, plan = route(files, chunk_budget_tokens=5_000)
        self.assertEqual(sc, SizeClass.LARGE)
        self.assertTrue(plan.chunks)
        self.assertTrue(all(c.est_tokens <= 5_000 for c in plan.chunks))


class TestFanOut(unittest.TestCase):
    def test_payloads_one_per_chunk(self):
        files = [FileChange(f"f{i}.py", 200, 0) for i in range(6)]
        _, _, plan = route(files, chunk_budget_tokens=3_000)
        jk = job_key_for("o/r", 7, "abc")
        payloads = build_chunk_payloads("o/r", 7, "abc", jk, plan)
        self.assertEqual(len(payloads), len(plan.chunks))
        self.assertTrue(all(p["event_type"] == CHUNK_EVENT and p["job_key"] == jk
                            for p in payloads))
        self.assertTrue(all("files" in p and p["files"] for p in payloads))


class TestFanIn(unittest.TestCase):
    def setUp(self):
        self.s = StateStore(":memory:")
        self.jk = "o/r#7@abc"
        self.s.create_job(self.jk, "abc", total_chunks=2)

    def test_claim_false_until_all_reported(self):
        self.s.record_chunk_finding(self.jk, 0, ["a.py"], "ок", True)
        self.assertFalse(claim_reduce(self.s, self.jk))  # 1 из 2

    def test_claim_single_winner(self):
        self.s.record_chunk_finding(self.jk, 0, ["a.py"], "ок", True)
        self.s.record_chunk_finding(self.jk, 1, ["b.py"], "", False)  # partial: fail
        self.assertTrue(claim_reduce(self.s, self.jk))   # все отчитались → выиграл
        self.assertFalse(claim_reduce(self.s, self.jk))  # повтор — уже запущен (M4)

    def test_collect_results(self):
        self.s.record_chunk_finding(self.jk, 0, ["a.py"], "f0", True)
        self.s.record_chunk_finding(self.jk, 1, ["b.py"], "", False)
        res = collect_results(self.s, self.jk)
        self.assertEqual([r.index for r in res], [0, 1])
        self.assertEqual(res[0].files, ("a.py",))
        self.assertFalse(res[1].ok)


if __name__ == "__main__":
    unittest.main()
