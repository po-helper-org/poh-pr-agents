"""Машина состояний и идемпотентность события (СТ-2, 10, 11, 12, 13, 16, 28).

SQLite-backed store (stdlib). На старте Фазы 1 достаточно embedded-хранилища;
переход на Postgres — отдельным срезом без изменения этого интерфейса.
"""
from __future__ import annotations

import enum
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional


class State(str, enum.Enum):
    RECEIVED = "received"
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


# СТ-13: терминальные состояния — событие не должно застревать вне них.
TERMINAL: frozenset[State] = frozenset({State.DONE, State.DEAD_LETTER})

# СТ-10: переходы только по разрешённому графу.
_ALLOWED: dict[State, frozenset[State]] = {
    State.RECEIVED: frozenset({State.QUEUED, State.FAILED}),
    State.QUEUED: frozenset({State.PROCESSING, State.FAILED}),
    State.PROCESSING: frozenset({State.DONE, State.FAILED}),
    State.FAILED: frozenset({State.QUEUED, State.DEAD_LETTER}),
    State.DONE: frozenset(),
    State.DEAD_LETTER: frozenset({State.QUEUED}),  # СТ-28: dead-letter не финал
}


class IllegalTransition(Exception):
    """Попытка перехода вне разрешённого графа (СТ-10)."""


@dataclass(frozen=True)
class Event:
    delivery_id: str          # X-GitHub-Delivery — ключ идемпотентности (СТ-2)
    repo: str
    number: int
    head_sha: str
    command: str
    event_type: str = "pull_request"

    @property
    def business_key(self) -> str:
        """СТ-11/16: идемпотентный эффект по (repo, number, head_sha, command).

        Новый пуш (иной head_sha) даёт иной ключ → ревью не дедупится, что верно.
        """
        return f"{self.repo}#{self.number}@{self.head_sha}:{self.command}"


def event_to_dict(event: "Event") -> dict:
    """Сериализация Event в payload очереди (СТ-8)."""
    return asdict(event)


def event_from_dict(d: dict) -> "Event":
    return Event(delivery_id=d["delivery_id"], repo=d["repo"], number=int(d["number"]),
                 head_sha=d["head_sha"], command=d["command"],
                 event_type=d.get("event_type", "pull_request"))


class StateStore:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        # check_same_thread=False + WAL + busy_timeout — готовность к worker pool
        # (СТ-5/14/17). Для реального многопроцессного доступа нужен файловый путь
        # (или Postgres); :memory: остаётся однопроцессным.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                delivery_id  TEXT PRIMARY KEY,
                business_key TEXT NOT NULL,
                repo         TEXT NOT NULL,
                number       INTEGER NOT NULL,
                head_sha     TEXT NOT NULL,
                command      TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                state        TEXT NOT NULL,
                attempts     INTEGER NOT NULL DEFAULT 0,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_business ON events(business_key);
            CREATE INDEX IF NOT EXISTS idx_events_state    ON events(state);
            CREATE TABLE IF NOT EXISTS reconcile (
                business_key TEXT PRIMARY KEY,
                cycles       INTEGER NOT NULL DEFAULT 0,
                updated_at   REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seq (id INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE IF NOT EXISTS claims (
                business_key TEXT PRIMARY KEY,
                delivery_id  TEXT NOT NULL,
                updated_at   REAL NOT NULL
            );
            """
        )
        self._db.commit()

    def record_received(self, e: Event) -> bool:
        """СТ-2: dedup по delivery_id. True — принято впервые, False — дубль доставки."""
        now = self._clock()
        try:
            self._db.execute(
                "INSERT INTO events(delivery_id,business_key,repo,number,head_sha,"
                "command,event_type,state,attempts,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                (e.delivery_id, e.business_key, e.repo, e.number, e.head_sha,
                 e.command, e.event_type, State.RECEIVED.value, now, now),
            )
            self._db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get(self, delivery_id: str) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM events WHERE delivery_id=?", (delivery_id,)
        ).fetchone()

    def state_of(self, delivery_id: str) -> Optional[State]:
        row = self.get(delivery_id)
        return State(row["state"]) if row else None

    def transition(self, delivery_id: str, to: State) -> None:
        """Перевести событие в новое состояние с валидацией графа (СТ-10)."""
        row = self.get(delivery_id)
        if row is None:
            raise KeyError(delivery_id)
        cur = State(row["state"])
        if to not in _ALLOWED[cur]:
            raise IllegalTransition(f"{cur.value} -> {to.value}")
        # CAS: пишем только если состояние не изменилось конкурентно между чтением
        # и записью (защита от lost-update при worker pool + sweeper, СТ-10/17).
        updated = self._db.execute(
            "UPDATE events SET state=?, updated_at=? WHERE delivery_id=? AND state=?",
            (to.value, self._clock(), delivery_id, cur.value),
        ).rowcount
        self._db.commit()
        if updated == 0:
            raise IllegalTransition(
                f"concurrent state change on {delivery_id}: expected {cur.value}"
            )

    def increment_attempt(self, delivery_id: str) -> int:
        """СТ-12: учёт попыток (для backoff и порога dead-letter)."""
        if self.get(delivery_id) is None:
            raise KeyError(delivery_id)
        self._db.execute(
            "UPDATE events SET attempts=attempts+1, updated_at=? WHERE delivery_id=?",
            (self._clock(), delivery_id),
        )
        self._db.commit()
        row = self.get(delivery_id)
        return int(row["attempts"]) if row else 0

    def already_done(self, business_key: str) -> bool:
        """СТ-16: результат для бизнес-ключа уже опубликован (идемпотентный эффект)."""
        return self._db.execute(
            "SELECT 1 FROM events WHERE business_key=? AND state=? LIMIT 1",
            (business_key, State.DONE.value),
        ).fetchone() is not None

    def stale(self, deadline_seconds: float) -> list[sqlite3.Row]:
        """СТ-13: события вне терминала, не обновлявшиеся дольше deadline."""
        cutoff = self._clock() - deadline_seconds
        placeholders = ",".join("?" for _ in TERMINAL)
        return self._db.execute(
            f"SELECT * FROM events WHERE state NOT IN ({placeholders}) AND updated_at < ?",
            (*[s.value for s in TERMINAL], cutoff),
        ).fetchall()

    def in_flight(self, business_key: str) -> bool:
        """СТ-30: есть ли событие с этим бизнес-ключом вне терминала (в работе)."""
        placeholders = ",".join("?" for _ in TERMINAL)
        return self._db.execute(
            f"SELECT 1 FROM events WHERE business_key=? AND state NOT IN ({placeholders}) LIMIT 1",
            (business_key, *[s.value for s in TERMINAL]),
        ).fetchone() is not None

    def bump_reconcile(self, business_key: str) -> int:
        """СТ-32: увеличить счётчик reconcile-циклов и вернуть новое значение."""
        now = self._clock()
        self._db.execute(
            "INSERT INTO reconcile(business_key,cycles,updated_at) VALUES(?,1,?) "
            "ON CONFLICT(business_key) DO UPDATE SET cycles=cycles+1, updated_at=?",
            (business_key, now, now),
        )
        self._db.commit()
        return int(self._db.execute(
            "SELECT cycles FROM reconcile WHERE business_key=?", (business_key,)
        ).fetchone()[0])

    def reconcile_cycles(self, business_key: str) -> int:
        row = self._db.execute(
            "SELECT cycles FROM reconcile WHERE business_key=?", (business_key,)
        ).fetchone()
        return int(row[0]) if row else 0

    def clear_reconcile(self, business_key: str) -> None:
        """Эффект подтверждён — обнулить счётчик (СТ-29)."""
        self._db.execute("DELETE FROM reconcile WHERE business_key=?", (business_key,))
        self._db.commit()

    def next_seq(self) -> int:
        """Монотонный ID, не повторяющийся даже после сбросов (для reconcile-id)."""
        cur = self._db.execute("INSERT INTO seq DEFAULT VALUES")
        self._db.commit()
        return int(cur.lastrowid)

    def try_claim(self, business_key: str, delivery_id: str) -> bool:
        """СТ-16: атомарный захват бизнес-ключа под анализ.

        True — ключ захвачен этим delivery_id (или уже держится им же, re-entrant);
        False — держит другой in-flight delivery → конкурентная доставка должна
        пропустить анализ, чтобы одна и та же работа не выполнилась дважды.
        Захват атомарен на уровне БД (INSERT ... ON CONFLICT DO NOTHING).
        """
        now = self._clock()
        inserted = self._db.execute(
            "INSERT INTO claims(business_key,delivery_id,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(business_key) DO NOTHING",
            (business_key, delivery_id, now),
        ).rowcount
        self._db.commit()
        if inserted:
            return True
        row = self._db.execute(
            "SELECT delivery_id FROM claims WHERE business_key=?", (business_key,)
        ).fetchone()
        return row is not None and row[0] == delivery_id

    def release_claim(self, business_key: str, delivery_id: str) -> None:
        """Освободить захват — только если держит именно этот delivery_id
        (не срываем чужой активный захват при гонке)."""
        self._db.execute(
            "DELETE FROM claims WHERE business_key=? AND delivery_id=?",
            (business_key, delivery_id),
        )
        self._db.commit()

    def claim_holder(self, business_key: str) -> Optional[str]:
        """delivery_id текущего держателя захвата, либо None (для диагностики/тестов)."""
        row = self._db.execute(
            "SELECT delivery_id FROM claims WHERE business_key=?", (business_key,)
        ).fetchone()
        return row[0] if row else None
