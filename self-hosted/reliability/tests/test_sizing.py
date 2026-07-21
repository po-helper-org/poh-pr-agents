"""ФТ-APRP-1 / НФТ-APRP-7: классификатор размера PR (чистые функции)."""
import unittest

from reliability.sizing import (
    FileChange,
    SizeClass,
    classify,
    diff_weight,
    estimate_tokens,
    model_token_budget,
)


class TestEstimate(unittest.TestCase):
    def test_tokens_grow_with_lines(self):
        self.assertGreater(estimate_tokens(100, 0), estimate_tokens(10, 0))

    def test_overhead_for_empty_change(self):
        self.assertEqual(estimate_tokens(0, 0), 40)  # только overhead файла

    def test_negative_clamped(self):
        self.assertEqual(estimate_tokens(-5, -5), 40)


class TestDiffWeight(unittest.TestCase):
    def test_sums_files_lines_tokens(self):
        files = [FileChange("a.py", 10, 2), FileChange("b.py", 5, 5)]
        w = diff_weight(files)
        self.assertEqual(w.files, 2)
        self.assertEqual(w.lines, 22)
        self.assertEqual(w.est_tokens, estimate_tokens(10, 2) + estimate_tokens(5, 5))

    def test_empty(self):
        w = diff_weight([])
        self.assertEqual((w.files, w.lines, w.est_tokens), (0, 0, 0))


class TestModelBudget(unittest.TestCase):
    def test_safe_fraction_minus_reserve(self):
        # 128k окно, 50% доля, 4k резерв → 60k
        self.assertEqual(model_token_budget(128_000, safe_frac=0.5, reserve_output=4000), 60_000)

    def test_never_negative(self):
        self.assertEqual(model_token_budget(1000, safe_frac=0.1, reserve_output=5000), 0)


class TestClassify(unittest.TestCase):
    def test_small_under_threshold(self):
        w = diff_weight([FileChange("a.py", 20, 5)])
        self.assertEqual(classify(w, large_threshold_tokens=10_000), SizeClass.SMALL)

    def test_large_over_token_threshold(self):
        w = diff_weight([FileChange("big.py", 5000, 0)])  # ~60k токенов
        self.assertEqual(classify(w, large_threshold_tokens=10_000), SizeClass.LARGE)

    def test_large_by_file_count(self):
        files = [FileChange(f"f{i}.py", 1, 0) for i in range(50)]
        w = diff_weight(files)
        self.assertEqual(classify(w, large_threshold_tokens=10_000_000, max_files=20),
                         SizeClass.LARGE)

    def test_file_limit_off_by_default(self):
        files = [FileChange(f"f{i}.py", 1, 0) for i in range(50)]
        w = diff_weight(files)
        self.assertEqual(classify(w, large_threshold_tokens=10_000_000), SizeClass.SMALL)


if __name__ == "__main__":
    unittest.main()
