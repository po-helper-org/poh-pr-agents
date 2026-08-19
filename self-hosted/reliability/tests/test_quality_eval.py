"""Q8/способ A: скоринг оценки качества — recall, false-positive, детекция."""
import unittest

from reliability.quality_eval import (
    DEFAULT_CASES,
    ReviewCase,
    caught_bug,
    evaluate,
    flagged_defect,
)


class TestDetection(unittest.TestCase):
    def test_caught_by_keyword(self):
        self.assertTrue(caught_bug("Возможна SQL-инъекция", ("инъекц", "sql")))
        self.assertFalse(caught_bug("всё хорошо", ("инъекц",)))

    def test_flagged_defect_true(self):
        self.assertTrue(flagged_defect("Здесь возможна утечка ресурса"))

    def test_no_issue_signal_not_flagged(self):
        self.assertFalse(flagged_defect("No issues found — код выглядит корректно"))
        self.assertFalse(flagged_defect("Нет замечаний"))


class TestEvaluate(unittest.TestCase):
    def test_perfect_model(self):
        # модель, «находящая» ровно ожидаемое и молчащая на чистом
        def model(system, user):
            u = user.lower()
            if "select" in u:
                return "SQL injection risk"
            if "open(" in u:
                return "resource leak: close the file"
            if "api_key" in u or "secret" in u:
                return "hardcoded secret"
            if ", _ :=" in user:
                return "ignored error"
            if "len(" in u:
                return "index out of range"
            if "user." in u:
                return "possible None deref"
            return "No issues found."
        rep = evaluate(DEFAULT_CASES, model)
        self.assertEqual(rep.recall, 1.0)               # все посаженные найдены
        self.assertEqual(rep.false_positive_rate, 0.0)  # на чистом — тихо

    def test_blind_model_low_recall(self):
        rep = evaluate(DEFAULT_CASES, lambda s, u: "No issues found.")
        self.assertEqual(rep.caught, 0)
        self.assertEqual(rep.recall, 0.0)
        self.assertEqual(rep.false_positive_rate, 0.0)  # молчит → нет FP

    def test_noisy_model_false_positives(self):
        # выдумывает баг всегда → FP на чистых кейсах
        rep = evaluate(DEFAULT_CASES, lambda s, u: "There is a bug and a leak here")
        self.assertGreater(rep.false_positives, 0)

    def test_report_partitions(self):
        rep = evaluate([
            ReviewCase("b", (("a.py", "select + x"),), "sql", ("sql",)),
            ReviewCase("c", (("m.py", "return a+b"),), "none", clean=True),
        ], lambda s, u: "sql injection" if "select" in u.lower() else "No issues found.")
        self.assertEqual(len(rep.seeded), 1)
        self.assertEqual(len(rep.clean_cases), 1)
        self.assertEqual(rep.recall, 1.0)


if __name__ == "__main__":
    unittest.main()
