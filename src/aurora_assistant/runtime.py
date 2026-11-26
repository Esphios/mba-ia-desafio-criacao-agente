from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import uuid4

from google.adk.events import Event
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, Session
from google.genai import types

from aurora_assistant.agents import build_agent_app
from aurora_assistant.config import Settings
from aurora_assistant.regulation import RegulationIndex
from aurora_assistant.storage import Repository
from aurora_assistant.tools import DomainTools


REQUEST_CONFIRMATION = "adk_request_confirmation"
APP_NAME = "aurora_assistant"


class SessionNotFoundError(LookupError):
    pass


class ConfirmationConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    id: str
    action: str
    details: dict[str, Any]
    invocation_id: str
    author: str
    original_call_id: str = ""

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "acao": self.action, "detalhes": self.details}


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    response: str
    pending: list[PendingConfirmation]


def build_confirmation_content(confirmation_id: str, confirmed: bool) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=confirmation_id,
                    name=REQUEST_CONFIRMATION,
                    response={"confirmed": confirmed},
                )
            )
        ],
    )


def find_pending_confirmations(events: Iterable[Event]) -> list[PendingConfirmation]:
    event_list = list(events)
    answered = {
        response.id
        for event in event_list
        for response in event.get_function_responses()
        if response.name == REQUEST_CONFIRMATION and response.id
    }
    pending: list[PendingConfirmation] = []
    for event in event_list:
        for call in event.get_function_calls():
            if call.name != REQUEST_CONFIRMATION or not call.id or call.id in answered:
                continue
            args = call.args or {}
            confirmation = args.get("toolConfirmation") or {}
            payload = confirmation.get("payload") or {}
            original = args.get("originalFunctionCall") or {}
            action = str(payload.get("acao") or original.get("name") or "confirmar_acao")
            details = payload.get("detalhes") or original.get("args") or {}
            pending.append(
                PendingConfirmation(
                    id=call.id,
                    action=action,
                    details=dict(details),
                    invocation_id=event.invocation_id,
                    author=event.author,
                    original_call_id=str(original.get("id") or call.id),
                )
            )
    return pending


def _last_text(events: Iterable[Event]) -> str:
    texts: list[str] = []
    for event in events:
        if not event.content:
            continue
        for part in event.content.parts or []:
            if part.text:
                texts.append(part.text)
    return texts[-1] if texts else ""


class AgentRuntime:
    def __init__(
        self, settings: Settings | None = None, *, app: App | None = None
    ) -> None:
        self.settings = settings or Settings()
        self.repository = Repository(self.settings.database_path)
        self.domain_tools = DomainTools(self.repository)
        self.regulation_index = RegulationIndex(
            self.settings.data_dir / "regulamento.md"
        )
        self.app = app or build_agent_app(
            self.settings, self.repository, self.regulation_index
        )
        self.session_service = DatabaseSessionService(self.settings.sqlalchemy_url)
        self.runner = Runner(app=self.app, session_service=self.session_service)
        self._confirmation_locks: dict[str, asyncio.Lock] = {}

    async def create_session(self, apartment: str) -> str:
        session_id = uuid4().hex
        self.repository.create_session(session_id, apartment)
        await self.session_service.create_session(
            app_name=APP_NAME,
            user_id=apartment,
            session_id=session_id,
            state={"apartamento": apartment},
        )
        return session_id

    async def get_session(self, session_id: str) -> Session | None:
        apartment = self.repository.get_session_apartment(session_id)
        if apartment is None:
            return None
        return await self.session_service.get_session(
            app_name=APP_NAME,
            user_id=apartment,
            session_id=session_id,
        )

    async def require_session(self, session_id: str) -> tuple[str, Session]:
        apartment = self.repository.get_session_apartment(session_id)
        if apartment is None:
            raise SessionNotFoundError(session_id)
        session = await self.session_service.get_session(
            app_name=APP_NAME, user_id=apartment, session_id=session_id
        )
        if session is None:
            raise SessionNotFoundError(session_id)
        return apartment, session

    async def send_message(self, session_id: str, text: str) -> RuntimeResult:
        apartment, _ = await self.require_session(session_id)
        message = types.Content(role="user", parts=[types.Part(text=text)])
        events = [
            event
            async for event in self.runner.run_async(
                user_id=apartment,
                session_id=session_id,
                new_message=message,
            )
        ]
        session = await self.get_session(session_id)
        return RuntimeResult(
            response=_last_text(events),
            pending=find_pending_confirmations(session.events if session else events),
        )

    async def respond_confirmation(
        self, session_id: str, confirmation_id: str, confirmed: bool
    ) -> RuntimeResult:
        lock = self._confirmation_locks.setdefault(confirmation_id, asyncio.Lock())
        async with lock:
            apartment, session = await self.require_session(session_id)
            pending = next(
                (
                    item
                    for item in find_pending_confirmations(session.events)
                    if item.id == confirmation_id
                ),
                None,
            )
            record = self.repository.get_confirmation(confirmation_id)
            if record is None:
                if pending is None:
                    raise ConfirmationConflictError(confirmation_id)
                record = self.repository.accept_confirmation(
                    session_id=session_id,
                    confirmation_id=confirmation_id,
                    confirmed=confirmed,
                    action=pending.action,
                    details=pending.details,
                    original_call_id=pending.original_call_id,
                    invocation_id=pending.invocation_id,
                )
            if (
                record is None
                or record.session_id != session_id
                or record.confirmed != confirmed
                or record.status == "completed"
            ):
                raise ConfirmationConflictError(confirmation_id)

            events: list[Event] = []
            recovery_response = "Confirmação processada."
            if pending is not None:
                events = [
                    event
                    async for event in self.runner.run_async(
                        user_id=apartment,
                        session_id=session_id,
                        invocation_id=record.invocation_id,
                        new_message=build_confirmation_content(
                            confirmation_id, confirmed
                        ),
                    )
                ]
            elif confirmed:
                recovery_response = self._reconcile_effect(apartment, record)
            else:
                recovery_response = "Ação recusada."

            self.repository.complete_confirmation(confirmation_id)
            refreshed = await self.get_session(session_id)
            return RuntimeResult(
                response=_last_text(events) or recovery_response,
                pending=find_pending_confirmations(refreshed.events if refreshed else events),
            )

    def _reconcile_effect(self, apartment: str, record: Any) -> str:
        """Finish an accepted action if ADK persisted its response before the tool."""
        action_key = (
            f"{record.session_id}:{record.invocation_id}:{record.original_call_id}"
        )
        result = self.domain_tools.reconcile_confirmed_action(
            apartment, record.action, record.details, action_key
        )
        if record.action == "reservar_area":
            if result["status"] == "created":
                return "Reserva concluída."
            if result["status"] == "unavailable":
                return "Área indisponível; nenhuma reserva foi criada."
            return "A reserva não pôde ser criada."
        elif record.action == "autorizar_visitante":
            return "Visitante autorizado."
        else:
            raise ConfirmationConflictError(record.confirmation_id)

    async def get_events(self, session_id: str) -> list[dict[str, Any]]:
        _, session = await self.require_session(session_id)
        return [
            event.model_dump(mode="json", by_alias=True, exclude_none=True)
            for event in session.events
        ]

    async def close(self) -> None:
        await self.runner.close()
