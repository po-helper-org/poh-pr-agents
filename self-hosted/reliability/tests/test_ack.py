"""ФТ-APRP-3: fast-ack — ETA, тело коммента, идемпотентная публикация."""
import unittest

from reliability.ack import (
    ACK_MARKER,
    SLA_LARGE_REVIEW_SEC,
    build_ack_comment,
    estimate_eta_seconds,
    publish_ack,
)
from reliability.chunking import plan_chunks
from reliability.sizing import FileChange, diff_weight


class FakeClient:
    def __init__(self):
        self.calls = []

    def upsert_comment(self, repo, number, marker, body):
        self.calls.append((repo, number, marker, body))


class TestEta(unittest.TestCase):
    def test_waves(self):
        # 7 чанков, параллелизм 3 → 3 волны × 40с = 120с
        self.assertEqual(estimate_eta_seconds(7, concurrency=3, per_chunk_sec=40), 120)

    def test_capped_at_sla(self):
        self.assertLessEqual(estimate_eta_seconds(1000, concurrency=1, per_chunk_sec=40),
                             SLA_LARGE_REVIEW_SEC)

    def test_zero_chunks(self):
        self.assertEqual(estimate_eta_seconds(0), 0)


class TestAckComment(unittest.TestCase):
    def _plan(self, n=5):
        files = [FileChange(f"f{i}.py", 50, 0) for i in range(n)]
        return diff_weight(files), plan_chunks(files, chunk_budget_tokens=700)

    def test_body_has_counts_and_marker(self):
        w, plan = self._plan(5)
        body = build_ack_comment(w, plan)
        self.assertIn("Большой PR", body)
        self.assertIn(f"**{w.files}**", body)
        self.assertTrue(body.strip().endswith(ACK_MARKER))

    def test_mentions_excluded_and_overflow(self):
        files = [FileChange("app.py", 50, 0), FileChange("package-lock.json", 999, 0)]
        w = diff_weight(files)
        plan = plan_chunks(files, chunk_budget_tokens=700)
        body = build_ack_comment(w, plan)
        self.assertIn("сгенерённое/вендорное", body)

    def test_publish_upserts_with_marker(self):
        w, plan = self._plan(3)
        c = FakeClient()
        publish_ack(c, "o/r", 7, w, plan)
        self.assertEqual(len(c.calls), 1)
        self.assertEqual(c.calls[0][:3], ("o/r", 7, ACK_MARKER))


if __name__ == "__main__":
    unittest.main()
