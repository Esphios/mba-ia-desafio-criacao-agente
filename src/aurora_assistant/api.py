from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from aurora_assistant.config import Settings
from aurora_assistant.runtime import (
    AgentRuntime,
    ConfirmationConflictError,
    RuntimeResult,
    SessionNotFoundError,
)
from aurora_assistant.storage import reset_database


class SessionCreate(BaseModel):
    apartamento: str


class SessionCreated(BaseModel):
    session_id: str


class MessageInput(BaseModel):
    texto: str


class ConfirmationInput(BaseModel):
    id: str
    confirmado: bool


class PendingOutput(BaseModel):
    id: str
    acao: str
    detalhes: dict[str, Any]


class ConversationOutput(BaseModel):
    resposta: str
    confirmacoes_pendentes: list[PendingOutput]


def _output(result: RuntimeResult) -> ConversationOutput:
    return ConversationOutput(
        resposta=result.response,
        confirmacoes_pendentes=[PendingOutput(**item.public()) for item in result.pending],
    )


def create_app(runtime: Any | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned_runtime = None
        if runtime is None:
            settings = Settings()
            if not settings.database_path.exists():
                reset_database(settings)
            owned_runtime = AgentRuntime(settings)
            app.state.runtime = owned_runtime
        try:
            yield
        finally:
            if owned_runtime is not None:
                await owned_runtime.close()

    app = FastAPI(title="Assistente do Residencial Aurora", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime

    def service(request: Request):
        return request.app.state.runtime

    @app.post(
        "/sessoes", response_model=SessionCreated, status_code=status.HTTP_201_CREATED
    )
    async def create_session(body: SessionCreate, request: Request) -> SessionCreated:
        session_id = await service(request).create_session(body.apartamento)
        return SessionCreated(session_id=session_id)

    @app.post(
        "/sessoes/{session_id}/mensagens", response_model=ConversationOutput
    )
    async def send_message(
        session_id: str, body: MessageInput, request: Request
    ) -> ConversationOutput:
        try:
            return _output(await service(request).send_message(session_id, body.texto))
        except SessionNotFoundError as error:
            raise HTTPException(status_code=404, detail="Sessão não encontrada") from error

    @app.post(
        "/sessoes/{session_id}/confirmacoes", response_model=ConversationOutput
    )
    async def answer_confirmation(
        session_id: str, body: ConfirmationInput, request: Request
    ) -> ConversationOutput:
        try:
            result = await service(request).respond_confirmation(
                session_id, body.id, body.confirmado
            )
            return _output(result)
        except SessionNotFoundError as error:
            raise HTTPException(status_code=404, detail="Sessão não encontrada") from error
        except ConfirmationConflictError as error:
            raise HTTPException(
                status_code=409, detail="Confirmação não está pendente nesta sessão"
            ) from error

    @app.get("/sessoes/{session_id}/eventos", response_model=list[dict[str, Any]])
    async def get_events(session_id: str, request: Request) -> list[dict[str, Any]]:
        try:
            return await service(request).get_events(session_id)
        except SessionNotFoundError as error:
            raise HTTPException(status_code=404, detail="Sessão não encontrada") from error

    @app.get("/apartamentos/{apartment}/reservas")
    async def get_reservations(apartment: str, request: Request) -> list[dict[str, str]]:
        return service(request).repository.list_reservations(apartment)

    @app.get("/apartamentos/{apartment}/visitantes")
    async def get_visitors(apartment: str, request: Request) -> list[dict[str, str]]:
        return service(request).repository.list_visitors(apartment)

    return app


app = create_app()
