from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date as calendar_date
from pathlib import Path
import re
from typing import Callable
from uuid import uuid4

from aurora_assistant.config import Settings


@dataclass(frozen=True, slots=True)
class OperationResult:
    ok: bool
    reason: str | None = None
    code: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmationRecord:
    confirmation_id: str
    session_id: str
    confirmed: bool
    status: str
    action: str
    details: dict
    original_call_id: str
    invocation_id: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS aurora_apartments (
    number TEXT PRIMARY KEY,
    resident TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aurora_areas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    fee REAL NOT NULL CHECK (fee >= 0)
);
CREATE TABLE IF NOT EXISTS aurora_reservations (
    code TEXT PRIMARY KEY,
    apartment TEXT NOT NULL REFERENCES aurora_apartments(number),
    area TEXT NOT NULL REFERENCES aurora_areas(id),
    reservation_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    cancelled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_aurora_active_area_date
ON aurora_reservations(area, reservation_date)
WHERE status = 'active';
CREATE TABLE IF NOT EXISTS aurora_visitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartment TEXT NOT NULL REFERENCES aurora_apartments(number),
    name TEXT NOT NULL,
    visit_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aurora_sessions (
    session_id TEXT PRIMARY KEY,
    apartment TEXT NOT NULL REFERENCES aurora_apartments(number),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS aurora_confirmation_responses (
    confirmation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES aurora_sessions(session_id),
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('accepted', 'completed')),
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    original_call_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    responded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS aurora_action_results (
    action_key TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def normalize_date(value: str) -> str:
    """Return a strict ISO calendar date or reject the model-supplied value."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("A data deve usar o formato YYYY-MM-DD")
    try:
        parsed = calendar_date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("A data deve usar o formato YYYY-MM-DD") from error
    canonical = parsed.isoformat()
    if canonical != value:
        raise ValueError("A data deve usar o formato YYYY-MM-DD")
    return canonical


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def reset_database(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    for database_path in (settings.database_path, settings.adk_database_path):
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{database_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    apartments = json.loads((settings.data_dir / "apartamentos.json").read_text("utf-8"))
    areas = json.loads((settings.data_dir / "areas.json").read_text("utf-8"))
    reservations = json.loads((settings.data_dir / "reservas.json").read_text("utf-8"))
    visitors = json.loads((settings.data_dir / "visitantes.json").read_text("utf-8"))

    with _connect(settings.database_path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "INSERT INTO aurora_apartments(number, resident) VALUES (?, ?)",
            ((item["numero"], item["morador"]) for item in apartments),
        )
        connection.executemany(
            "INSERT INTO aurora_areas(id, name, fee) VALUES (?, ?, ?)",
            ((item["id"], item["nome"], item["taxa"]) for item in areas),
        )
        connection.executemany(
            """INSERT INTO aurora_reservations
               (code, apartment, area, reservation_date) VALUES (?, ?, ?, ?)""",
            (
                (item["codigo"], item["apartamento"], item["area"], item["data"])
                for item in reservations
            ),
        )
        connection.executemany(
            "INSERT INTO aurora_visitors(apartment, name, visit_date) VALUES (?, ?, ?)",
            ((item["apartamento"], item["nome"], item["data"]) for item in visitors),
        )
        connection.commit()


class Repository:
    def __init__(
        self,
        database_path: Path,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self._code_factory = code_factory or (
            lambda: f"RSV-{uuid4().hex[:12].upper()}"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.database_path) as connection:
            connection.executescript(SCHEMA)

    def list_reservations(self, apartment: str) -> list[dict[str, str]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT code, area, reservation_date
                   FROM aurora_reservations
                   WHERE apartment = ? AND status = 'active'
                   ORDER BY reservation_date, code""",
                (apartment,),
            ).fetchall()
        return [
            {"codigo": row["code"], "area": row["area"], "data": row["reservation_date"]}
            for row in rows
        ]

    def list_visitors(self, apartment: str) -> list[dict[str, str]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT name, visit_date FROM aurora_visitors
                   WHERE apartment = ? ORDER BY visit_date, id""",
                (apartment,),
            ).fetchall()
        return [{"nome": row["name"], "data": row["visit_date"]} for row in rows]

    def create_session(self, session_id: str, apartment: str) -> None:
        with _connect(self.database_path) as connection:
            existing = connection.execute(
                "SELECT apartment FROM aurora_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing:
                if existing["apartment"] != apartment:
                    raise ValueError("A sessão já pertence a outro apartamento")
                return
            connection.execute(
                "INSERT INTO aurora_sessions(session_id, apartment) VALUES (?, ?)",
                (session_id, apartment),
            )

    def get_session_apartment(self, session_id: str) -> str | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT apartment FROM aurora_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row["apartment"] if row else None

    def area_fee(self, area: str) -> float | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT fee FROM aurora_areas WHERE id = ?", (area,)
            ).fetchone()
        return float(row["fee"]) if row else None

    def resolve_area(self, value: str) -> str | None:
        """Resolve um ID ou nome humano de área para o ID canônico."""

        def comparable(text: str) -> str:
            ascii_text = unicodedata.normalize("NFKD", text).encode(
                "ascii", "ignore"
            ).decode("ascii")
            return "-".join(ascii_text.casefold().replace("_", " ").split()).strip("-")

        candidate = comparable(value)
        with _connect(self.database_path) as connection:
            rows = connection.execute("SELECT id, name FROM aurora_areas").fetchall()
        for row in rows:
            if candidate in {comparable(row["id"]), comparable(row["name"])}:
                return str(row["id"])
        return None

    def is_available(self, area: str, date: str) -> bool:
        date = normalize_date(date)
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT 1 FROM aurora_reservations
                   WHERE area = ? AND reservation_date = ? AND status = 'active'""",
                (area, date),
            ).fetchone()
        return row is None

    def create_reservation(
        self,
        apartment: str,
        area: str,
        date: str,
        *,
        action_key: str | None = None,
    ) -> OperationResult:
        date = normalize_date(date)
        code = self._code_factory()
        request = json.dumps(
            {"apartment": apartment, "area": area, "date": date},
            sort_keys=True,
        )
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if action_key:
                prior = connection.execute(
                    """SELECT action, request_json, result_json
                       FROM aurora_action_results WHERE action_key = ?""",
                    (action_key,),
                ).fetchone()
                if prior:
                    if prior["action"] != "create_reservation" or prior["request_json"] != request:
                        raise RuntimeError("Chave idempotente reutilizada com outra ação")
                    connection.rollback()
                    return OperationResult(**json.loads(prior["result_json"]))

            connection.execute("SAVEPOINT reservation_insert")
            try:
                connection.execute(
                    """INSERT INTO aurora_reservations
                       (code, apartment, area, reservation_date) VALUES (?, ?, ?, ?)""",
                    (code, apartment, area, date),
                )
            except sqlite3.IntegrityError as error:
                # Roll back only the reservation statement. The outer write
                # transaction stays open, so an unavailable outcome and its
                # idempotency record commit atomically with no crash gap.
                connection.execute("ROLLBACK TO SAVEPOINT reservation_insert")
                connection.execute("RELEASE SAVEPOINT reservation_insert")
                message = str(error)
                if "area" in message and "reservation_date" in message:
                    result = OperationResult(ok=False, reason="unavailable")
                elif "code" in message:
                    result = OperationResult(ok=False, reason="duplicate_code")
                else:
                    connection.rollback()
                    raise
                if action_key:
                    connection.execute(
                        """INSERT INTO aurora_action_results
                           (action_key, action, request_json, result_json)
                           VALUES (?, 'create_reservation', ?, ?)""",
                        (
                            action_key,
                            request,
                            json.dumps(
                                {"ok": result.ok, "reason": result.reason, "code": result.code}
                            ),
                        ),
                    )
                connection.commit()
                return result
            else:
                connection.execute("RELEASE SAVEPOINT reservation_insert")
                result = OperationResult(ok=True, code=code)
                if action_key:
                    connection.execute(
                        """INSERT INTO aurora_action_results
                           (action_key, action, request_json, result_json)
                           VALUES (?, 'create_reservation', ?, ?)""",
                        (action_key, request, json.dumps({"ok": True, "reason": None, "code": code})),
                    )
                connection.commit()
                return result

    def cancel_reservation(self, apartment: str, area: str, date: str) -> OperationResult:
        date = normalize_date(date)
        with _connect(self.database_path) as connection:
            cursor = connection.execute(
                """UPDATE aurora_reservations
                   SET status = 'cancelled', cancelled_at = CURRENT_TIMESTAMP
                   WHERE apartment = ? AND area = ? AND reservation_date = ?
                     AND status = 'active'""",
                (apartment, area, date),
            )
        return OperationResult(ok=cursor.rowcount == 1, reason=None if cursor.rowcount else "not_found")

    def add_visitor(
        self,
        apartment: str,
        name: str,
        date: str,
        *,
        action_key: str | None = None,
    ) -> OperationResult:
        date = normalize_date(date)
        request = json.dumps(
            {"apartment": apartment, "name": name, "date": date},
            sort_keys=True,
        )
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if action_key:
                prior = connection.execute(
                    """SELECT action, request_json, result_json
                       FROM aurora_action_results WHERE action_key = ?""",
                    (action_key,),
                ).fetchone()
                if prior:
                    if prior["action"] != "authorize_visitor" or prior["request_json"] != request:
                        raise RuntimeError("Chave idempotente reutilizada com outra ação")
                    connection.rollback()
                    return OperationResult(**json.loads(prior["result_json"]))
            connection.execute(
                "INSERT INTO aurora_visitors(apartment, name, visit_date) VALUES (?, ?, ?)",
                (apartment, name, date),
            )
            if action_key:
                connection.execute(
                    """INSERT INTO aurora_action_results
                       (action_key, action, request_json, result_json)
                       VALUES (?, 'authorize_visitor', ?, ?)""",
                    (action_key, request, json.dumps({"ok": True, "reason": None, "code": None})),
                )
            connection.commit()
        return OperationResult(ok=True)

    def accept_confirmation(
        self,
        session_id: str,
        confirmation_id: str,
        confirmed: bool,
        action: str,
        details: dict,
        original_call_id: str,
        invocation_id: str,
    ) -> ConfirmationRecord | None:
        with _connect(self.database_path) as connection:
            try:
                connection.execute(
                    """INSERT INTO aurora_confirmation_responses
                       (confirmation_id, session_id, confirmed, status, action,
                        details_json, original_call_id, invocation_id)
                       VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?)""",
                    (
                        confirmation_id,
                        session_id,
                        int(confirmed),
                        action,
                        json.dumps(details),
                        original_call_id,
                        invocation_id,
                    ),
                )
                return self.get_confirmation(confirmation_id)
            except sqlite3.IntegrityError:
                return self.get_confirmation(confirmation_id)

    def get_confirmation(self, confirmation_id: str) -> ConfirmationRecord | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT confirmation_id, session_id, confirmed, status, action,
                          details_json, original_call_id, invocation_id
                   FROM aurora_confirmation_responses
                   WHERE confirmation_id = ?""",
                (confirmation_id,),
            ).fetchone()
        if row is None:
            return None
        return ConfirmationRecord(
            confirmation_id=row["confirmation_id"],
            session_id=row["session_id"],
            confirmed=bool(row["confirmed"]),
            status=row["status"],
            action=row["action"],
            details=json.loads(row["details_json"]),
            original_call_id=row["original_call_id"],
            invocation_id=row["invocation_id"],
        )

    def complete_confirmation(self, confirmation_id: str) -> None:
        with _connect(self.database_path) as connection:
            connection.execute(
                """UPDATE aurora_confirmation_responses
                   SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                   WHERE confirmation_id = ? AND status = 'accepted'""",
                (confirmation_id,),
            )
