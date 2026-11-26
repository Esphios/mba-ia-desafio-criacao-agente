import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from aurora_assistant.config import Settings
from aurora_assistant.agents import build_agent_app
from aurora_assistant.regulation import RegulationIndex
from aurora_assistant.runtime import AgentRuntime, build_confirmation_content, find_pending_confirmations
from aurora_assistant.storage import Repository, reset_database
from aurora_assistant.tools import DomainTools


class ScriptedModel(BaseLlm):
    responses: list[types.Content]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del llm_request, stream
        yield LlmResponse(content=self.responses.pop(0))


def _app(repository: Repository, responses: list[types.Content]) -> App:
    tools = DomainTools(repository)
    agent = LlmAgent(
        name="reservas",
        model=ScriptedModel(model="scripted", responses=responses),
        instruction="Use a tool.",
        tools=[tools.reserve_area],
    )
    return App(
        name="aurora_assistant",
        root_agent=agent,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )


@pytest.mark.asyncio
async def test_native_adk_confirmation_resumes_after_runner_restart(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(settings)
    repository = Repository(settings.database_path)
    service = DatabaseSessionService(settings.sqlalchemy_url)
    await service.create_session(
        app_name="aurora_assistant",
        user_id="101",
        session_id="session-adk",
        state={"apartamento": "101"},
    )
    call = types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    id="original-tool-call",
                    name="reserve_area",
                    args={"area": "salao-de-festas", "date": "2030-04-20"},
                )
            )
        ],
    )
    first_runner = Runner(
        app=_app(repository, [call]), session_service=service
    )
    first_events = [
        event
        async for event in first_runner.run_async(
            user_id="101",
            session_id="session-adk",
            new_message=types.Content(role="user", parts=[types.Part(text="reserve")]),
        )
    ]
    session = await service.get_session(
        app_name="aurora_assistant", user_id="101", session_id="session-adk"
    )
    pending = find_pending_confirmations(session.events)
    assert len(pending) == 1
    assert not any(item["data"] == "2030-04-20" for item in repository.list_reservations("101"))
    await first_runner.close()

    # A new service/runner proves that the confirmation is resumed from SQLite,
    # not from an in-memory callback.
    restarted_service = DatabaseSessionService(settings.sqlalchemy_url)
    final_text = types.Content(role="model", parts=[types.Part(text="Reserva concluída.")])
    restarted_runner = Runner(
        app=_app(repository, [final_text]), session_service=restarted_service
    )
    resumed_events = [
        event
        async for event in restarted_runner.run_async(
            user_id="101",
            session_id="session-adk",
            invocation_id=pending[0].invocation_id,
            new_message=build_confirmation_content(pending[0].id, True),
        )
    ]

    assert any("Reserva concluída" in (part.text or "") for event in resumed_events if event.content for part in (event.content.parts or []))
    created = [item for item in repository.list_reservations("101") if item["data"] == "2030-04-20"]
    assert len(created) == 1
    refreshed = await restarted_service.get_session(
        app_name="aurora_assistant", user_id="101", session_id="session-adk"
    )
    assert find_pending_confirmations(refreshed.events) == []
    await restarted_runner.close()


@pytest.mark.asyncio
async def test_agent_runtime_routes_concierge_to_specialist_and_resumes(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(settings)
    repository = Repository(settings.database_path)
    scripted = {
        "concierge": [
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="transfer-1",
                            name="transfer_to_agent",
                            args={"agent_name": "reservas"},
                        )
                    )
                ],
            )
        ],
        "reservas": [
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="reserve-actual-topology",
                            name="reserve_area",
                            args={"area": "salao-de-festas", "date": "2030-04-20"},
                        )
                    )
                ],
            )
        ],
        "visitantes": [],
        "regulamento": [],
    }
    app = build_agent_app(
        settings,
        repository,
        RegulationIndex(settings.data_dir / "regulamento.md"),
        model_factory=lambda name: ScriptedModel(model="scripted", responses=scripted[name]),
    )
    first = AgentRuntime(settings, app=app)
    session_id = await first.create_session("101")

    pending_result = await first.send_message(session_id, "Reserve o salão")

    assert len(pending_result.pending) == 1
    assert pending_result.pending[0].author == "reservas"
    await first.close()

    restarted_repository = Repository(settings.database_path)
    restarted_app = build_agent_app(
        settings,
        restarted_repository,
        RegulationIndex(settings.data_dir / "regulamento.md"),
        model_factory=lambda name: ScriptedModel(
            model="scripted",
            responses=(
                [types.Content(role="model", parts=[types.Part(text="Reserva concluída.")])]
                if name == "reservas"
                else []
            ),
        ),
    )
    restarted = AgentRuntime(settings, app=restarted_app)
    result = await restarted.respond_confirmation(
        session_id, pending_result.pending[0].id, True
    )

    assert result.response == "Reserva concluída."
    assert sum(
        item["data"] == "2030-04-20"
        for item in restarted.repository.list_reservations("101")
    ) == 1
    await restarted.close()


@pytest.mark.asyncio
async def test_concurrent_runtime_approvals_return_normally_and_create_once(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(settings)
    repository = Repository(settings.database_path)

    def call(call_id: str, name: str, args: dict) -> types.Content:
        return types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id=call_id,
                        name=name,
                        args=args,
                    )
                )
            ],
        )

    scripted = {
        "concierge": [
            call("transfer-101", "transfer_to_agent", {"agent_name": "reservas"}),
            call("transfer-201", "transfer_to_agent", {"agent_name": "reservas"}),
        ],
        "reservas": [
            call(
                "reserve-101",
                "reserve_area",
                {"area": "salao-de-festas", "date": "2030-05-11"},
            ),
            call(
                "reserve-201",
                "reserve_area",
                {"area": "salao-de-festas", "date": "2030-05-11"},
            ),
            types.Content(role="model", parts=[types.Part(text="Concluído.")]),
            types.Content(role="model", parts=[types.Part(text="Concluído.")]),
        ],
        "visitantes": [],
        "regulamento": [],
    }
    app = build_agent_app(
        settings,
        repository,
        RegulationIndex(settings.data_dir / "regulamento.md"),
        model_factory=lambda name: ScriptedModel(
            model="scripted", responses=scripted[name]
        ),
    )
    runtime = AgentRuntime(settings, app=app)
    session_101 = await runtime.create_session("101")
    session_201 = await runtime.create_session("201")
    first = await runtime.send_message(session_101, "Reserve o salão")
    second = await runtime.send_message(session_201, "Reserve o salão")

    results = await asyncio.gather(
        runtime.respond_confirmation(session_101, first.pending[0].id, True),
        runtime.respond_confirmation(session_201, second.pending[0].id, True),
    )

    assert all(result.response == "Concluído." for result in results)
    created = [
        item
        for apartment in ("101", "201")
        for item in repository.list_reservations(apartment)
        if item["area"] == "salao-de-festas" and item["data"] == "2030-05-11"
    ]
    assert len(created) == 1
    await runtime.close()
