from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from aurora_assistant.api import create_app
from aurora_assistant.config import Settings
from aurora_assistant.runtime import (
    ConfirmationConflictError,
    PendingConfirmation,
    RuntimeResult,
    SessionNotFoundError,
)
from aurora_assistant.storage import Repository, reset_database


class FakeRuntime:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def create_session(self, apartment: str) -> str:
        self.repository.create_session("session-1", apartment)
        return "session-1"

    async def send_message(self, session_id: str, text: str) -> RuntimeResult:
        if self.repository.get_session_apartment(session_id) is None:
            raise SessionNotFoundError(session_id)
        return RuntimeResult(
            response="",
            pending=[
                PendingConfirmation(
                    id="confirm-1",
                    action="reservar_area",
                    details={"area": "salao-de-festas", "data": "2030-04-20"},
                    invocation_id="inv-1",
                    author="reservas",
                )
            ],
        )

    async def respond_confirmation(
        self, session_id: str, confirmation_id: str, confirmed: bool
    ) -> RuntimeResult:
        if self.repository.get_session_apartment(session_id) is None:
            raise SessionNotFoundError(session_id)
        if confirmation_id != "confirm-1":
            raise ConfirmationConflictError(confirmation_id)
        return RuntimeResult(response="Reserva recusada", pending=[])

    async def get_events(self, session_id: str) -> list[dict]:
        if self.repository.get_session_apartment(session_id) is None:
            raise SessionNotFoundError(session_id)
        return [{"id": "event-1", "author": "concierge"}]


@pytest.fixture
async def client(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(settings)
    app = create_app(FakeRuntime(Repository(settings.database_path)))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


@pytest.mark.asyncio
async def test_create_session_and_contract(client: AsyncClient) -> None:
    response = await client.post("/sessoes", json={"apartamento": "101"})

    assert response.status_code == 201
    assert response.json() == {"session_id": "session-1"}

    message = await client.post(
        "/sessoes/session-1/mensagens",
        json={"texto": "Reserve o salão em 2030-04-20"},
    )
    assert message.status_code == 200
    assert message.json() == {
        "resposta": "",
        "confirmacoes_pendentes": [
            {
                "id": "confirm-1",
                "acao": "reservar_area",
                "detalhes": {"area": "salao-de-festas", "data": "2030-04-20"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_confirmation_returns_409_for_non_pending_id(client: AsyncClient) -> None:
    await client.post("/sessoes", json={"apartamento": "101"})

    response = await client.post(
        "/sessoes/session-1/confirmacoes",
        json={"id": "missing", "confirmado": True},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_unknown_session_routes_return_404(client: AsyncClient) -> None:
    message = await client.post(
        "/sessoes/missing/mensagens", json={"texto": "Olá"}
    )
    confirmation = await client.post(
        "/sessoes/missing/confirmacoes", json={"id": "x", "confirmado": True}
    )
    events = await client.get("/sessoes/missing/eventos")

    assert [message.status_code, confirmation.status_code, events.status_code] == [
        404,
        404,
        404,
    ]


@pytest.mark.asyncio
async def test_verification_routes_read_database_directly(client: AsyncClient) -> None:
    reservations = await client.get("/apartamentos/101/reservas")
    visitors = await client.get("/apartamentos/302/visitantes")

    assert reservations.status_code == 200
    assert reservations.json() == [
        {"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"}
    ]
    assert visitors.json() == [
        {"nome": "Marina Duarte", "data": "2030-03-16"}
    ]


@pytest.mark.asyncio
async def test_events_are_returned_complete_and_ordered(client: AsyncClient) -> None:
    await client.post("/sessoes", json={"apartamento": "101"})

    response = await client.get("/sessoes/session-1/eventos")

    assert response.status_code == 200
    assert response.json() == [{"id": "event-1", "author": "concierge"}]
