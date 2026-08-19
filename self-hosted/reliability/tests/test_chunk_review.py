"""ФТ-APRP-7: ревью чанка — промпт, вызов модели (seam), пустой чанк, проброс сбоя."""
import unittest

from reliability.chunk_review import (
    SYSTEM_PROMPT,
    build_review_prompt,
    patches_for_files,
    review_chunk,
)


class TestPrompt(unittest.TestCase):
    def test_includes_paths_patches_instructions(self):
        system, user = build_review_prompt(
            [("a.py", "@@ -1 +1 @@\n-x\n+y")], extra_instructions="INSTR")
        self.assertIn(SYSTEM_PROMPT, system)
        self.assertIn("INSTR", system)          # инструкции — в system, не в user
        self.assertNotIn("INSTR", user)
        self.assertIn("`a.py`", user)
        self.assertIn("+y", user)

    def test_skips_empty_patches(self):
        _, user = build_review_prompt([("bin.png", ""), ("a.py", "patch")])
        self.assertNotIn("bin.png", user)
        self.assertIn("a.py", user)


class TestReviewChunk(unittest.TestCase):
    def test_calls_model_and_returns_findings(self):
        seen = {}

        def model_call(system, user):
            seen["system"], seen["user"] = system, user
            return "  Найден баг в a.py  "

        out = review_chunk(model_call, [("a.py", "patch")])
        self.assertEqual(out, "Найден баг в a.py")   # обрезка
        self.assertIn("a.py", seen["user"])

    def test_empty_chunk_no_model_call(self):
        called = []
        out = review_chunk(lambda s, u: called.append(1) or "x",
                           [("bin.png", ""), ("data.bin", "")])
        self.assertEqual(called, [])                 # модель не звали
        self.assertIn("Нет текстовых изменений", out)

    def test_model_failure_propagates(self):
        def boom(system, user):
            raise RuntimeError("z.ai timeout")
        with self.assertRaises(RuntimeError):
            review_chunk(boom, [("a.py", "patch")])


class TestPatchesForFiles(unittest.TestCase):
    def test_selects_chunk_patches(self):
        class FakeClient:
            def list_pull_files(self, repo, number):
                return [{"filename": "a.py", "patch": "PA"},
                        {"filename": "b.py", "patch": "PB"},
                        {"filename": "c.py", "patch": "PC"}]

        got = patches_for_files(FakeClient(), "o/r", 7, ["a.py", "c.py"])
        self.assertEqual(got, [("a.py", "PA"), ("c.py", "PC")])

    def test_missing_file_empty_patch(self):
        class FakeClient:
            def list_pull_files(self, repo, number):
                return [{"filename": "a.py", "patch": "PA"}]

        got = patches_for_files(FakeClient(), "o/r", 7, ["a.py", "gone.py"])
        self.assertEqual(got, [("a.py", "PA"), ("gone.py", "")])


if __name__ == "__main__":
    unittest.main()
