"""ФТ-APRP-8 / НФТ-APRP-6: reduce-синтез — сборка, «не отревьюено», glue, публикация."""
import unittest

from reliability.reduce import REVIEW_MARKER, ChunkResult, publish_review, synthesize


class FakeClient:
    def __init__(self):
        self.calls = []

    def upsert_comment(self, repo, number, marker, body):
        self.calls.append((repo, number, marker, body))


class TestSynthesize(unittest.TestCase):
    def test_all_ok(self):
        results = [
            ChunkResult(0, ("a.py",), "Всё ок в a"),
            ChunkResult(1, ("b.py",), "Замечание по b"),
        ]
        body = synthesize(results)
        self.assertIn("2/2 чанков готово", body)
        self.assertIn("Замечание по b", body)
        self.assertNotIn("Не отревьюено", body)
        self.assertTrue(body.strip().endswith(REVIEW_MARKER))

    def test_failed_chunk_listed(self):
        results = [
            ChunkResult(0, ("a.py",), "ок"),
            ChunkResult(1, ("b.py", "c.py"), "", ok=False),
        ]
        body = synthesize(results)
        self.assertIn("1/2 чанков готово", body)
        self.assertIn("Не отревьюено", body)
        self.assertIn("`b.py`", body)
        self.assertIn("`c.py`", body)

    def test_overflow_listed(self):
        results = [ChunkResult(0, ("a.py",), "ок")]
        body = synthesize(results, overflow_files=["big.py"])
        self.assertIn("`big.py`", body)
        self.assertIn("вне бюджета", body)

    def test_glue_applied(self):
        results = [ChunkResult(0, ("a.py",), "raw")]
        body = synthesize(results, glue=lambda s: s.upper())
        self.assertIn("RAW", body)

    def test_no_findings(self):
        results = [ChunkResult(0, ("a.py",), "", ok=False)]
        body = synthesize(results)
        self.assertIn("0/1 чанков готово", body)
        self.assertIn("Не отревьюено", body)

    def test_publish_upserts_with_marker(self):
        c = FakeClient()
        publish_review(c, "o/r", 7, [ChunkResult(0, ("a.py",), "ок")])
        self.assertEqual(c.calls[0][:3], ("o/r", 7, REVIEW_MARKER))


if __name__ == "__main__":
    unittest.main()
