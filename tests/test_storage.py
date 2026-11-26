from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import aurora_assistant.storage as storage_module
from aurora_assistant.config import Settings
from aurora_assistant.storage import Repository, reset_database


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    project_root = Path(__file__).parents[1]
    settings = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=project_root / "dados",
    )
    reset_database(settings)
    return Repository(settings.database_path)


def test_reset_loads_initial_reservations_and_visitors(repository: Repository) -> None:
    assert repository.list_reservations("101") == [
        {"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"}
    ]
    assert repository.list_visitors("302") == [
        {"nome": "Marina Duarte", "data": "2030-03-16"}
    ]


def test_session_apartment_is_immutable(repository: Repository) -> None:
    repository.create_session("session-1", "101")

    assert repository.get_session_apartment("session-1") == "101"
    with pytest.raises(ValueError, match="já pertence"):
        repository.create_session("session-1", "302")


def test_reservation_and_visitor_operations_are_scoped(repository: Repository) -> None:
    cancelled = repository.cancel_reservation("101", "quadra", "2030-03-09")
    forbidden = repository.cancel_reservation("101", "salao-de-festas", "2030-03-16")
    created = repository.create_reservation("101", "quadra", "2030-04-06")
    visitor = repository.add_visitor("101", "Joana Ribeiro", "2030-04-21")

    assert cancelled.ok is True
    assert forbidden.ok is False
    assert forbidden.reason == "not_found"
    assert created.ok is True
    assert visitor.ok is True
    assert repository.list_reservations("302")[0]["codigo"] == "RSV-4821"
    assert repository.list_visitors("101") == [
        {"nome": "Joana Ribeiro", "data": "2030-04-21"}
    ]


def test_cancelled_reservation_code_is_never_reused(repository: Repository) -> None:
    repository.cancel_reservation("101", "quadra", "2030-03-09")

    first = repository.create_reservation("101", "quadra", "2030-04-06")
    second = repository.create_reservation("101", "quadra", "2030-04-07")

    assert first.code and second.code
    assert len({first.code, second.code, "RSV-1377"}) == 3


def test_concurrent_reservations_have_exactly_one_winner(repository: Repository) -> None:
    barrier = Barrier(2)

    def reserve(apartment: str):
        barrier.wait()
        return repository.create_reservation(
            apartment, "salao-de-festas", "2030-05-11"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["101", "201"]))

    assert sorted(result.ok for result in results) == [False, True]
    assert {result.reason for result in results} == {None, "unavailable"}
    total = sum(
        reservation["data"] == "2030-05-11"
        for apartment in ("101", "201")
        for reservation in repository.list_reservations(apartment)
    )
    assert total == 1


def test_availability_and_area_fee_do_not_expose_occupant(repository: Repository) -> None:
    assert repository.resolve_area("salão de festas") == "salao-de-festas"
    assert repository.resolve_area("Salao de Festas") == "salao-de-festas"
    assert repository.resolve_area("quadra") == "quadra"
    assert repository.resolve_area("área inventada") is None
    assert repository.is_available("salao-de-festas", "2030-03-16") is False
    assert repository.area_fee("salao-de-festas") == 150.0
    assert repository.area_fee("quadra") == 0.0


def test_dates_are_strict_iso_and_cannot_bypass_exclusivity(repository: Repository) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        repository.create_reservation("101", "quadra", "2030-4-6")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        repository.add_visitor("101", "Joana Ribeiro", "2030-02-30")

    assert repository.create_reservation("101", "quadra", "2030-04-06").ok
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        repository.is_available("quadra", "2030-4-6")


def test_confirmed_effects_are_idempotent_by_original_tool_call(repository: Repository) -> None:
    first = repository.create_reservation(
        "101", "salao-de-festas", "2030-04-20", action_key="tool-call-1"
    )
    replay = repository.create_reservation(
        "101", "salao-de-festas", "2030-04-20", action_key="tool-call-1"
    )
    repository.add_visitor(
        "101", "Joana Ribeiro", "2030-04-21", action_key="tool-call-2"
    )
    repository.add_visitor(
        "101", "Joana Ribeiro", "2030-04-21", action_key="tool-call-2"
    )

    assert first == replay
    assert sum(item["data"] == "2030-04-20" for item in repository.list_reservations("101")) == 1
    assert repository.list_visitors("101").count(
        {"nome": "Joana Ribeiro", "data": "2030-04-21"}
    ) == 1

    with pytest.raises(RuntimeError, match="outra ação"):
        repository.add_visitor(
            "302", "Outra Pessoa", "2030-04-22", action_key="tool-call-1"
        )


def test_unavailable_result_is_idempotent_even_after_winner_cancels(repository: Repository) -> None:
    assert repository.create_reservation("302", "quadra", "2030-04-30").ok
    refused = repository.create_reservation(
        "101", "quadra", "2030-04-30", action_key="session:invocation:call"
    )
    assert refused.reason == "unavailable"
    assert repository.cancel_reservation("302", "quadra", "2030-04-30").ok

    replay = repository.create_reservation(
        "101", "quadra", "2030-04-30", action_key="session:invocation:call"
    )

    assert replay == refused
    assert not any(item["data"] == "2030-04-30" for item in repository.list_reservations("101"))


def test_unavailable_outcome_is_recorded_in_same_write_transaction(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert repository.create_reservation("302", "quadra", "2030-05-01").ok
    statements: list[str] = []
    original_connect = storage_module._connect

    def traced_connect(path: Path):
        connection = original_connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(storage_module, "_connect", traced_connect)
    result = repository.create_reservation(
        "101", "quadra", "2030-05-01", action_key="session:invocation:atomic"
    )

    normalized = [statement.upper() for statement in statements]
    begin = next(index for index, statement in enumerate(normalized) if "BEGIN IMMEDIATE" in statement)
    savepoint_rollback = next(index for index, statement in enumerate(normalized) if "ROLLBACK TO SAVEPOINT" in statement)
    outcome_insert = next(
        index
        for index, statement in enumerate(normalized)
        if "INSERT INTO AURORA_ACTION_RESULTS" in statement
    )
    commit = next(index for index, statement in enumerate(normalized) if statement.strip() == "COMMIT")

    assert result.reason == "unavailable"
    assert begin < savepoint_rollback < outcome_insert < commit
    assert not any(statement.strip() == "ROLLBACK" for statement in normalized)
    assert sum("BEGIN IMMEDIATE" in statement for statement in normalized) == 1
