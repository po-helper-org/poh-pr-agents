"""ФТ-APRP-6/8 (M3/M4): job-стор fan-in — findings в сторе, идемпотентность, CAS reduce."""
import unittest

from reliability.state import StateStore


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.s = StateStore(":memory:")
        self.s.create_job("o/r#7@abc", "abc", total_chunks=3)

    def test_create_idempotent(self):
        self.assertFalse(self.s.create_job("o/r#7@abc", "abc", 3))  # повтор — не создаёт
        self.assertEqual(self.s.job_status("o/r#7@abc")["total_chunks"], 3)

    def test_record_and_counters(self):
        self.s.record_chunk_finding("o/r#7@abc", 0, ["a.py"], "ок", True)
        self.s.record_chunk_finding("o/r#7@abc", 1, ["b.py"], "", False)  # сбой
        st = self.s.job_status("o/r#7@abc")
        self.assertEqual(st["done_chunks"], 2)
        self.assertEqual(st["failed_chunks"], 1)
        self.assertFalse(self.s.job_all_reported("o/r#7@abc"))  # 2 из 3

    def test_record_idempotent_per_chunk(self):
        self.s.record_chunk_finding("o/r#7@abc", 0, ["a.py"], "v1", True)
        self.s.record_chunk_finding("o/r#7@abc", 0, ["a.py"], "v2", True)  # передоставка
        st = self.s.job_status("o/r#7@abc")
        self.assertEqual(st["done_chunks"], 1)                 # не задвоилось
        self.assertEqual(self.s.job_findings("o/r#7@abc")[0][2], "v2")  # перезаписалось

    def test_all_reported(self):
        for i in range(3):
            self.s.record_chunk_finding("o/r#7@abc", i, [f"f{i}"], "ок", True)
        self.assertTrue(self.s.job_all_reported("o/r#7@abc"))

    def test_try_start_reduce_cas(self):
        self.assertTrue(self.s.try_start_reduce("o/r#7@abc"))   # победитель
        self.assertFalse(self.s.try_start_reduce("o/r#7@abc"))  # второй — no-op (M4)

    def test_findings_ordered(self):
        self.s.record_chunk_finding("o/r#7@abc", 2, ["c"], "f2", True)
        self.s.record_chunk_finding("o/r#7@abc", 0, ["a"], "f0", True)
        idx = [f[0] for f in self.s.job_findings("o/r#7@abc")]
        self.assertEqual(idx, [0, 2])

    def test_status_none_for_unknown(self):
        self.assertIsNone(self.s.job_status("nope"))


if __name__ == "__main__":
    unittest.main()
