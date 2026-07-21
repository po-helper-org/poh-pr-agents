"""ФТ-APRP-4/5: планировщик чанков — исключения, приоритет, упаковка, overflow."""
import unittest

from reliability.chunking import (
    Priority,
    file_priority,
    is_excluded,
    plan_chunks,
)
from reliability.sizing import FileChange, estimate_tokens


class TestExclude(unittest.TestCase):
    def test_generated_vendored_excluded(self):
        for p in ("node_modules/x.js", "package-lock.json", "app.min.js",
                  "proto/a_pb2.py", "vendor/lib.go", "poetry.lock"):
            self.assertTrue(is_excluded(p), p)

    def test_source_not_excluded(self):
        for p in ("src/app.py", "reliability/worker.py", "README.md"):
            self.assertFalse(is_excluded(p), p)


class TestPriority(unittest.TestCase):
    def test_ranks(self):
        self.assertEqual(file_priority("src/app.py"), Priority.CORE)
        self.assertEqual(file_priority("tests/test_x.py"), Priority.TESTS)
        self.assertEqual(file_priority("pkg/foo_test.go"), Priority.TESTS)
        self.assertEqual(file_priority("pyproject.toml"), Priority.CONFIG)
        self.assertEqual(file_priority(".github/workflows/ci.yml"), Priority.CONFIG)
        self.assertEqual(file_priority("docs/guide.md"), Priority.DOCS)
        self.assertEqual(file_priority("README.md"), Priority.DOCS)


class TestPlanChunks(unittest.TestCase):
    def test_excluded_reported_not_chunked(self):
        files = [FileChange("app.py", 5, 0), FileChange("package-lock.json", 999, 0)]
        plan = plan_chunks(files, chunk_budget_tokens=10_000)
        self.assertEqual(plan.excluded, ["package-lock.json"])
        allfiles = [f.path for c in plan.chunks for f in c.files]
        self.assertEqual(allfiles, ["app.py"])

    def test_packs_within_budget(self):
        # каждый файл = 5*12+40 = 100 токенов; бюджет 200 → по 2 в чанк
        files = [FileChange(f"{c}.py", 5, 0) for c in "abc"]
        plan = plan_chunks(files, chunk_budget_tokens=200)
        self.assertEqual(len(plan.chunks), 2)
        self.assertEqual([len(c.files) for c in plan.chunks], [2, 1])
        self.assertTrue(all(c.est_tokens <= 200 for c in plan.chunks))

    def test_oversized_file_own_chunk(self):
        files = [FileChange("big.py", 100, 0)]  # 1240 токенов > бюджета
        plan = plan_chunks(files, chunk_budget_tokens=200)
        self.assertEqual(len(plan.chunks), 1)
        self.assertTrue(plan.chunks[0].oversized)

    def test_priority_order_core_before_docs(self):
        files = [FileChange("guide.md", 5, 0), FileChange("app.py", 5, 0)]
        plan = plan_chunks(files, chunk_budget_tokens=1000)
        order = [f.path for c in plan.chunks for f in c.files]
        self.assertEqual(order, ["app.py", "guide.md"])  # core раньше docs

    def test_overflow_drops_lowest_priority(self):
        # общий бюджет вмещает только core; docs — overflow
        core = FileChange("app.py", 5, 0)      # 100 токенов
        doc = FileChange("guide.md", 5, 0)     # 100 токенов
        plan = plan_chunks([doc, core], chunk_budget_tokens=1000, total_budget_tokens=100)
        chunked = [f.path for c in plan.chunks for f in c.files]
        self.assertEqual(chunked, ["app.py"])          # важное отревьюено
        self.assertEqual(plan.overflow_skipped, ["guide.md"])  # низший приоритет — с пометкой


if __name__ == "__main__":
    unittest.main()
