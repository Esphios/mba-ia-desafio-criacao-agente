from pathlib import Path

from google.adk.models import Gemini

from aurora_assistant.agents import build_agent_app
from aurora_assistant.config import Settings
from aurora_assistant.regulation import RegulationIndex
from aurora_assistant.storage import Repository, reset_database


def test_agent_topology_has_concierge_and_three_specialists(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(settings)

    app = build_agent_app(
        settings,
        Repository(settings.database_path),
        RegulationIndex(settings.data_dir / "regulamento.md"),
    )

    root = app.root_agent
    assert root.name == "concierge"
    assert {agent.name for agent in root.sub_agents} == {
        "reservas",
        "visitantes",
        "regulamento",
    }
    assert app.resumability_config.is_resumable is True


def test_concierge_instruction_does_not_contain_regulation_text(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(settings)
    app = build_agent_app(
        settings,
        Repository(settings.database_path),
        RegulationIndex(settings.data_dir / "regulamento.md"),
    )

    root_instruction = str(app.root_agent.instruction)
    regulation = (settings.data_dir / "regulamento.md").read_text("utf-8")

    assert "Capítulo IV: Piscina" not in root_instruction
    assert regulation[:200] not in root_instruction
    assert app.root_agent.tools == []


def test_dotenv_api_key_is_passed_to_every_gemini_model(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        GOOGLE_API_KEY="secret-from-dotenv",
        GEMINI_MODEL="gemini-test",
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(settings)

    app = build_agent_app(
        settings,
        Repository(settings.database_path),
        RegulationIndex(settings.data_dir / "regulamento.md"),
    )

    agents = [app.root_agent, *app.root_agent.sub_agents]
    assert all(isinstance(agent.model, Gemini) for agent in agents)
    assert all(agent.model.model == "gemini-test" for agent in agents)
    assert all(
        agent.model.client_kwargs["api_key"] == "secret-from-dotenv"
        for agent in agents
    )
    assert all(agent.model.retry_options.attempts == 5 for agent in agents)
    assert all(agent.model.retry_options.max_delay == 8 for agent in agents)
    assert all(agent.model.client_kwargs["http_options"].timeout == 90_000 for agent in agents)
