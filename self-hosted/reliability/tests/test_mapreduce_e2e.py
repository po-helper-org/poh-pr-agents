"""Сквозной прогон map-reduce конвейера (ФТ-APRP-1..10) — все компоненты вместе,
с фейковыми моделью и GitHub. Доказывает, что движок собирается и работает end-to-end
без ошибок (реальный GLM-5 закрывается отдельным live-смоуком — chunk_review.glm_model_call)."""
import unittest

from reliability import ack, mapreduce, reduce
from reliability.chunk_review import patches_for_files, review_chunk
from reliability.sizing import files_from_api
from reliability.state import StateStore


class FakeGitHub:
    """list_pull_files + upsert_comment; пишет опубликованные комменты по маркеру."""
    def __init__(self, files):
        self._files = files
        self.comments = {}  # marker -> body

    def list_pull_files(self, repo, number):
        return self._files

    def upsert_comment(self, repo, number, marker, body):
        self.comments[marker] = body


def fake_model(system, user):
    # «модель» находит по одному замечанию на файл, упомянутый в промпте
    import re
    files = re.findall(r"`([^`]+)`", user)
    return "\n".join(f"- {f}: возможна проблема с обработкой ошибок" for f in files)


class TestMapReduceEndToEnd(unittest.TestCase):
    def test_full_flow_large_pr(self):
        repo, number, sha = "kibarik/app", 42, "deadbeef"
        # большой PR: 12 файлов по ~100 строк + один сгенерённый (исключается)
        raw = [{"filename": f"src/mod{i}.py", "additions": 100, "deletions": 20,
                "status": "modified", "patch": f"@@ mod{i} @@\n+code{i}"} for i in range(12)]
        raw.append({"filename": "package-lock.json", "additions": 9999, "deletions": 0,
                    "patch": "@@ lock @@"})
        gh = FakeGitHub(raw)
        store = StateStore(":memory:")

        # 1) классификация + план (chunk_budget мал → несколько чанков)
        files = files_from_api(raw)
        sc, weight, plan = mapreduce.route(files, chunk_budget_tokens=5000)
        self.assertEqual(sc.value, "large")
        self.assertTrue(plan.chunks)
        self.assertIn("package-lock.json", plan.excluded)   # сгенерённое исключено

        # 2) fast-ack (ФТ-APRP-3)
        ack.publish_ack(gh, repo, number, weight, plan)
        self.assertIn(ack.ACK_MARKER, gh.comments)
        self.assertIn("Большой PR", gh.comments[ack.ACK_MARKER])

        # 3) job + fan-out + ревью каждого чанка (ФТ-APRP-6/7)
        jk = mapreduce.job_key_for(repo, number, sha)
        store.create_job(jk, sha, total_chunks=len(plan.chunks))
        payloads = mapreduce.build_chunk_payloads(repo, number, sha, jk, plan)
        self.assertEqual(len(payloads), len(plan.chunks))
        for p in payloads:
            fwp = patches_for_files(gh, repo, number, p["files"])
            findings = review_chunk(fake_model, fwp)
            store.record_chunk_finding(jk, p["chunk_index"], p["files"], findings, ok=True)
            # прогресс (ФТ-APRP-9)
            st = store.job_status(jk)
            ack.publish_progress(gh, repo, number, weight, plan,
                                 done=st["done_chunks"], failed=st["failed_chunks"])

        # 4) fan-in → reduce → публикация (ФТ-APRP-8, идемпотентно M4)
        self.assertTrue(mapreduce.claim_reduce(store, jk))
        self.assertFalse(mapreduce.claim_reduce(store, jk))   # второй reduce не запустится
        results = mapreduce.collect_results(store, jk)
        reduce.publish_review(gh, repo, number, results)

        # итог: единый review-коммент со всеми файлами, без «не отревьюено»
        self.assertIn(reduce.REVIEW_MARKER, gh.comments)
        review = gh.comments[reduce.REVIEW_MARKER]
        self.assertIn("чанков готово", review)
        self.assertNotIn("Не отревьюено", review)
        for i in range(12):
            self.assertIn(f"src/mod{i}.py", review)          # каждый файл отревьюён

    def test_partial_flow_with_failed_chunk(self):
        # один чанк «упал» → partial-reduce с явной пометкой (НФТ-APRP-6)
        repo, number, sha = "kibarik/app", 7, "abc"
        raw = [{"filename": f"m{i}.py", "additions": 300, "deletions": 0,
                "patch": f"+c{i}"} for i in range(4)]
        gh = FakeGitHub(raw)
        store = StateStore(":memory:")
        files = files_from_api(raw)
        _, weight, plan = mapreduce.route(files, chunk_budget_tokens=3000)
        jk = mapreduce.job_key_for(repo, number, sha)
        store.create_job(jk, sha, total_chunks=len(plan.chunks))
        for i, p in enumerate(mapreduce.build_chunk_payloads(repo, number, sha, jk, plan)):
            ok = i != 0                                       # первый чанк «упал»
            findings = "ок" if ok else ""
            store.record_chunk_finding(jk, p["chunk_index"], p["files"], findings, ok=ok)
        self.assertTrue(mapreduce.claim_reduce(store, jk))
        reduce.publish_review(gh, repo, number, mapreduce.collect_results(store, jk))
        review = gh.comments[reduce.REVIEW_MARKER]
        self.assertIn("Не отревьюено", review)                # непройденное явно помечено


if __name__ == "__main__":
    unittest.main()
