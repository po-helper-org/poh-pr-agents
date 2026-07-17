"""Оркестрация адаптера: запуск анализа и проброс ошибки (без pr-agent)."""
import unittest

from reliability.analyze_adapter import PRAgentAnalyzer, _pr_url
from reliability.state import Event


def ev():
    return Event(delivery_id="d", repo="o/r", number=7, head_sha="abc", command="/review")


class TestAnalyzer(unittest.TestCase):
    def test_pr_url(self):
        self.assertEqual(_pr_url(ev()), "https://github.com/o/r/pull/7")

    def test_run_invokes_with_url_command_and_repo(self):
        calls = []
        a = PRAgentAnalyzer(invoke=lambda url, cmd, repo: calls.append((url, cmd, repo)))
        a.run(ev())
        self.assertEqual(calls, [("https://github.com/o/r/pull/7", "/review", "o/r")])

    def test_run_propagates_error(self):
        def boom(url, cmd, repo):
            raise RuntimeError("llm down")
        a = PRAgentAnalyzer(invoke=boom)
        with self.assertRaises(RuntimeError):  # → супервизор → dead-letter → коммент
            a.run(ev())


if __name__ == "__main__":
    unittest.main()
