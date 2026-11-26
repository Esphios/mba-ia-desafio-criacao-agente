from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.events import Event
from google.genai import types

from aurora_assistant.config import Settings
from aurora_assistant.runtime import (
    AgentRuntime,
    build_confirmation_content,
    find_pending_confirmations,
)
from aurora_assistant.storage import reset_database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(value)
    return value


@pytest.mark.asyncio
async def test_adk_session_survives_runtime_recreation(settings: Settings) -> None:
    first = AgentRuntime(settings)
    session_id = await first.create_session("101")
    await first.close()

    second = AgentRuntime(settings)
    session = await second.get_session(session_id)

    assert session is not None
    assert session.state["apartamento"] == "101"
    assert second.repository.get_session_apartment(session_id) == "101"
    await second.close()


@pytest.mark.asyncio
async def test_accepted_confirmation_recovers_if_response_preceded_tool(settings: Settings) -> None:
    first = AgentRuntime(settings)
    session_id = await first.create_session("101")
    session = await first.get_session(session_id)
    request = Event(
        invocation_id="inv-crash",
        author="reservas",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="confirm-crash",
                        name="adk_request_confirmation",
                        args={
                            "originalFunctionCall": {
                                "id": "tool-crash",
                                "name": "reserve_area",
                                "args": {"area": "salao-de-festas", "date": "2030-04-20"},
                            },
                            "toolConfirmation": {
                                "payload": {
                                    "acao": "reservar_area",
                                    "detalhes": {
                                        "area": "salao-de-festas",
                                        "data": "2030-04-20",
                                        "taxa": 150.0,
                                    },
                                }
                            },
                        },
                    )
                )
            ],
        ),
    )
    await first.session_service.append_event(session=session, event=request)
    first.repository.accept_confirmation(
        session_id,
        "confirm-crash",
        True,
        "reservar_area",
        {"area": "salao-de-festas", "data": "2030-04-20", "taxa": 150.0},
        "tool-crash",
        "inv-crash",
    )
    await first.session_service.append_event(
        session=session,
        event=Event(
            invocation_id="inv-crash",
            author="user",
            content=build_confirmation_content("confirm-crash", True),
        ),
    )
    await first.close()

    restarted = AgentRuntime(settings)
    result = await restarted.respond_confirmation(session_id, "confirm-crash", True)

    assert result.response == "Reserva concluída."
    created = [
        item
        for item in restarted.repository.list_reservations("101")
        if item["data"] == "2030-04-20"
    ]
    assert len(created) == 1
    with pytest.raises(Exception):
        await restarted.respond_confirmation(session_id, "confirm-crash", True)
    await restarted.close()


@pytest.mark.asyncio
async def test_recovery_reports_and_preserves_unavailable_outcome(settings: Settings) -> None:
    runtime = AgentRuntime(settings)
    assert runtime.repository.create_reservation(
        "302", "salao-de-festas", "2030-04-30"
    ).ok
    record = SimpleNamespace(
        session_id="session-101",
        invocation_id="invocation-1",
        original_call_id="call-1",
        confirmation_id="confirmation-1",
        action="reservar_area",
        details={"area": "salao-de-festas", "data": "2030-04-30", "taxa": 150.0},
    )

    first = runtime._reconcile_effect("101", record)
    runtime.repository.cancel_reservation("302", "salao-de-festas", "2030-04-30")
    replay = runtime._reconcile_effect("101", record)

    assert first == replay == "Área indisponível; nenhuma reserva foi criada."
    assert not any(
        item["data"] == "2030-04-30"
        for item in runtime.repository.list_reservations("101")
    )
    await runtime.close()


def test_pending_confirmation_preserves_public_details_and_invocation() -> None:
    request = Event(
        invocation_id="inv-1",
        author="reservas",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="confirm-1",
                        name="adk_request_confirmation",
                        args={
                            "originalFunctionCall": {
                                "id": "tool-1",
                                "name": "reserve_area",
                                "args": {"area": "salao-de-festas", "date": "2030-04-20"},
                            },
                            "toolConfirmation": {
                                "hint": "Confirmar?",
                                "payload": {
                                    "acao": "reservar_area",
                                    "detalhes": {
                                        "area": "salao-de-festas",
                                        "data": "2030-04-20",
                                    },
                                },
                            },
                        },
                    )
                )
            ],
        ),
        long_running_tool_ids={"confirm-1"},
    )

    pending = find_pending_confirmations([request])

    assert len(pending) == 1
    assert pending[0].id == "confirm-1"
    assert pending[0].action == "reservar_area"
    assert pending[0].details == {
        "area": "salao-de-festas",
        "data": "2030-04-20",
    }
    assert pending[0].invocation_id == "inv-1"


def test_answered_confirmation_is_not_pending() -> None:
    request = Event(
        invocation_id="inv-1",
        author="visitantes",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="confirm-1",
                        name="adk_request_confirmation",
                        args={"toolConfirmation": {"payload": {"acao": "autorizar_visitante", "detalhes": {}}}},
                    )
                )
            ],
        ),
    )
    response = Event(
        invocation_id="inv-1",
        author="user",
        content=build_confirmation_content("confirm-1", True),
    )

    assert find_pending_confirmations([request, response]) == []


def test_confirmation_content_matches_adk_protocol() -> None:
    content = build_confirmation_content("confirm-7", False)
    function_response = content.parts[0].function_response

    assert content.role == "user"
    assert function_response.id == "confirm-7"
    assert function_response.name == "adk_request_confirmation"
    assert function_response.response == {"confirmed": False}
