"""Измерение качества ревью «посевными багами» (способ A, закрывает Q8).

Берём набор патчей с ЗАРАНЕЕ известными дефектами, прогоняем ревью и объективно
считаем: сколько посаженных багов найдено (recall) и сколько ложных срабатываний на
ЧИСТОМ коде (false positives). Оценщику (PO) не нужно judge'ить код — метрика число.

Детекция «нашла баг» — эвристика по ключевым словам ожидаемой находки (простая и
объективная; для более строгой оценки — модель-судья, способ B, followup). Скоринг
чистый/тестируемый; сам вызов модели инъектируется (фейк в тестах / GLM-5 на live).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reliability.chunk_review import review_chunk


@dataclass(frozen=True)
class ReviewCase:
    name: str
    files: tuple              # ((path, patch), ...)
    seeded_bug: str           # что посажено (для отчёта)
    expect_keywords: tuple = ()   # находка должна упомянуть хотя бы одно (для seeded)
    clean: bool = False       # True — дефектов нет, ревью НЕ должно выдумывать


@dataclass(frozen=True)
class CaseResult:
    name: str
    clean: bool
    passed: bool              # seeded: нашла баг; clean: не выдумала дефект
    findings: str


@dataclass
class EvalReport:
    results: list = field(default_factory=list)

    @property
    def seeded(self):
        return [r for r in self.results if not r.clean]

    @property
    def clean_cases(self):
        return [r for r in self.results if r.clean]

    @property
    def caught(self):
        return sum(1 for r in self.seeded if r.passed)

    @property
    def recall(self) -> float:
        return self.caught / len(self.seeded) if self.seeded else 1.0

    @property
    def false_positives(self):
        return sum(1 for r in self.clean_cases if not r.passed)

    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / len(self.clean_cases) if self.clean_cases else 0.0


# сигналы «замечаний нет» — чтобы на чистом коде не счесть похвалу за дефект
NO_ISSUE_SIGNALS = (
    "no issue", "no issues", "looks correct", "looks good", "no defect", "no problem",
    "lgtm", "нет замечаний", "корректно", "выглядит корректно", "проблем не",
)
DEFECT_WORDS = (
    "bug", "vulnerab", "injection", "leak", "race", "overflow", "unsafe", "insecure",
    "null", "none", "index", "out of range", "error handling", "unchecked", "missing",
    "hardcoded", "secret", "credential", "deadlock", "off-by-one", "incorrect",
    "баг", "уязвим", "утечк", "инъекц", "гонк", "переполн", "секрет",
)


def caught_bug(findings: str, keywords) -> bool:
    f = findings.lower()
    return any(k.lower() in f for k in keywords)


def flagged_defect(findings: str) -> bool:
    """Эвристика: ревью УТВЕРЖДАЕТ дефект (для FP на чистом коде). Сначала гасим
    'замечаний нет', иначе ищем маркеры дефекта."""
    f = findings.lower()
    if any(s in f for s in NO_ISSUE_SIGNALS):
        return False
    return any(w in f for w in DEFECT_WORDS)


def evaluate(cases, model_call, *, review_fn=review_chunk) -> EvalReport:
    """Прогнать кейсы через ревью и оценить. `model_call(system,user)->str` — фейк/GLM-5."""
    rep = EvalReport()
    for c in cases:
        findings = review_fn(model_call, list(c.files))
        if c.clean:
            passed = not flagged_defect(findings)
        else:
            passed = caught_bug(findings, c.expect_keywords)
        rep.results.append(CaseResult(c.name, c.clean, passed, findings))
    return rep


# ── Датасет посевных багов (иллюстративный; команда расширяет под свой стек) ──
DEFAULT_CASES = (
    ReviewCase(
        "sql-injection",
        (("db.py", "@@ +q @@\n+query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"\n"
                   "+cursor.execute(query)"),),
        "SQL-инъекция: конкатенация пользовательского ввода в запрос",
        ("sql", "injection", "инъекц", "parameter"),
    ),
    ReviewCase(
        "ignored-error-go",
        (("h.go", "@@ +f @@\n+data, _ := ioutil.ReadFile(path)\n+process(data)"),),
        "Проигнорирована ошибка чтения файла (err = _)",
        ("error", "err", "ignored", "unchecked", "ошибк"),
    ),
    ReviewCase(
        "resource-leak",
        (("io.py", "@@ +f @@\n+f = open(path)\n+data = f.read()\n+return data  # f не закрыт"),),
        "Утечка ресурса: файл не закрывается (нет with/close)",
        ("leak", "close", "resource", "утечк", "закры", "context manager", "with"),
    ),
    ReviewCase(
        "hardcoded-secret",
        (("cfg.py", "@@ +s @@\n+API_KEY = \"sk-live-abc123secret\"\n+client = Client(API_KEY)"),),
        "Захардкоженный секрет в коде",
        ("secret", "hardcoded", "credential", "key", "секрет", "ключ"),
    ),
    ReviewCase(
        "index-out-of-range",
        (("arr.py", "@@ +f @@\n+def last(xs):\n+    return xs[len(xs)]  # off-by-one"),),
        "Выход за границу массива: xs[len(xs)]",
        ("index", "out of range", "off-by-one", "bound", "границ", "индекс"),
    ),
    ReviewCase(
        "none-deref",
        (("u.py", "@@ +f @@\n+user = find_user(id)\n+return user.name  # user может быть None"),),
        "Разыменование None: user может быть None",
        ("none", "null", "check", "проверк", "разымен"),
    ),
    # чистые кейсы — дефектов нет, ревью НЕ должно выдумывать
    ReviewCase(
        "clean-add",
        (("m.py", "@@ +f @@\n+def add(a, b):\n+    return a + b"),),
        "нет бага (корректная функция)",
        clean=True,
    ),
    ReviewCase(
        "clean-guard",
        (("s.py", "@@ +f @@\n+def head(xs):\n+    if not xs:\n+        return None\n+    return xs[0]"),),
        "нет бага (корректная проверка пустоты)",
        clean=True,
    ),
)
