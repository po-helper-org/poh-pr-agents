"""Доклад PR-Agent в цикл Issue (H3).

Проверяем две вещи, на которых держится контур: событие несёт ключ задачи, и
недоступный сосед не превращает выполненное ревью в сбой.
"""
from __future__ import annotations

import json

from reliability import agent_event


ENV = {"ISSUE_AGENT_URL": "https://issue-agent.example/", "AGENT_EVENT_SECRET": "s3cret"}


class _Recorder:
    """Транспорт-шпион: запоминает вызов, отвечает заданным кодом."""

    def __init__(self, code=200, boom: Exception | None = None):
        self.code, self.boom, self.calls = code, boom, []

    def __call__(self, method, url, data, headers):
        if self.boom:
            raise self.boom
        self.calls.append((method, url, data, headers))
        return self.code, b"{}"


# --- сквозной ключ ---

def test_root_issue_comes_from_closes():
    assert agent_event.parse_root_issue("Closes #42\n\nтекст") == 42
    assert agent_event.parse_root_issue("fixes #7") == 7


def test_several_closed_issues_leave_the_key_unset():
    """Выбрать первый попавшийся значит привязать работу не к той задаче — и
    испортить трассировку сразу двум."""
    assert agent_event.parse_root_issue("Closes #1\nCloses #2") is None


def test_no_closes_is_not_an_error():
    assert agent_event.parse_root_issue("просто описание") is None
    assert agent_event.parse_root_issue(None) is None


# --- конверт ---

def test_payload_names_the_phase_this_service_owns():
    payload = agent_event.build_payload("o/r", 12, agent_event.STARTED, root_issue=7)

    assert payload["agent"] == "pr-agent"
    assert payload["phase"] == "pr-review"
    assert payload["ref"] == "12"
    assert payload["root_issue"] == 7


def test_missing_root_issue_is_omitted_not_nulled():
    """`root_issue: null` цикл разобрал бы как «ключ передан и он пустой».
    Отсутствие поля означает «не знаю», и корреляция идёт дальше по телу."""
    assert "root_issue" not in agent_event.build_payload("o/r", 12, agent_event.STARTED)


# --- отправка ---

def test_signature_covers_exactly_the_sent_body():
    transport = _Recorder()

    agent_event.report("o/r", 12, agent_event.STARTED, root_issue=7,
                       env=ENV, transport=transport)

    _method, url, data, headers = transport.calls[0]
    assert url == "https://issue-agent.example/agent-event"
    assert headers["X-Agent-Signature-256"] == agent_event.sign("s3cret", data)
    assert json.loads(data)["root_issue"] == 7


def test_unconfigured_channel_sends_nothing():
    """Пустой адрес или секрет — это и есть процедура отката."""
    transport = _Recorder()

    assert agent_event.report("o/r", 12, agent_event.STARTED, env={}, transport=transport) is False
    assert transport.calls == []


def test_unreachable_neighbour_does_not_raise():
    """Ревью уже опубликовано. Обменять сделанную работу на несделанный доклад
    нельзя — сбой отправки остаётся сбоем отправки."""
    transport = _Recorder(boom=OSError("connection refused"))

    assert agent_event.report("o/r", 12, agent_event.STARTED,
                              env=ENV, transport=transport) is False


def test_rejected_event_does_not_raise():
    transport = _Recorder(code=401)

    assert agent_event.report("o/r", 12, agent_event.STARTED,
                              env=ENV, transport=transport) is False


# --- врезка в воркер ---

class _Client:
    def __init__(self, body="Closes #7"):
        self.body = body

    def get_pull_body(self, repo, number):
        return self.body


def _worker_reports(monkeypatch, command: str, client=None) -> list:
    """Гоняет врезку воркера, возвращая перехваченные доклады."""
    from reliability import worker
    from reliability.state import Event

    sent: list = []
    monkeypatch.setattr(agent_event, "configured", lambda *a, **k: True)
    monkeypatch.setattr(agent_event, "report",
                        lambda repo, n, status, **kw: sent.append((repo, n, status, kw)))

    worker._report_to_issue_cycle(client or _Client(), Event("d1", "o/r", 7, "abc", command),
                                  agent_event.STARTED)
    return sent


def test_review_moves_the_issue_phase(monkeypatch):
    sent = _worker_reports(monkeypatch, "/review")

    assert sent and sent[0][2] == agent_event.STARTED
    assert sent[0][3]["root_issue"] == 7


def test_describe_does_not_move_the_issue_phase(monkeypatch):
    """`/describe` переписывает описание PR. Доложить о нём как о ревью значило
    бы сдвинуть фазу задачи работой, которой не было."""
    assert _worker_reports(monkeypatch, "/describe") == []


def test_broken_pr_lookup_does_not_break_the_worker(monkeypatch):
    class Broken:
        def get_pull_body(self, repo, number):
            raise RuntimeError("GitHub 502")

    assert _worker_reports(monkeypatch, "/review", client=Broken()) == []


def test_client_without_pull_lookup_does_not_break_the_worker(monkeypatch):
    """Свипер и тесты подсовывают клиентов поуже — отсутствие метода не сбой."""
    assert _worker_reports(monkeypatch, "/review", client=object()) == []
