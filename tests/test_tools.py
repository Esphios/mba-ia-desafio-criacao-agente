import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest

from aurora_assistant.config import Settings
from aurora_assistant.storage import Repository, reset_database
from aurora_assistant.tools import DomainTools


@dataclass
class FakeConfirmation:
    confirmed: bool


class FakeToolContext:
    def __init__(
        self,
        apartment: str,
        confirmation: FakeConfirmation | None = None,
        function_call_id: str = "tool-call-test",
    ):
        self.state = {"apartamento": apartment}
        self.tool_confirmation = confirmation
        self.function_call_id = function_call_id
        self.requested: dict | None = None

    def request_confirmation(self, *, hint=None, payload=None) -> None:
        self.requested = {"hint": hint, "payload": payload}


@pytest.fixture
def tools(tmp_path: Path) -> DomainTools:
    settings = Settings(
        _env_file=None,
        AURORA_DATABASE_PATH=tmp_path / "aurora.db",
        data_dir=Path(__file__).parents[1] / "dados",
    )
    reset_database(settings)
    return DomainTools(Repository(settings.database_path))


def test_private_tools_do_not_accept_apartment_argument() -> None:
    for name in (
        "list_my_reservations",
        "list_my_visitors",
        "reserve_area",
        "cancel_my_reservation",
        "authorize_visitor",
    ):
        assert "apartment" not in inspect.signature(getattr(DomainTools, name)).parameters


def test_reads_and_cancellation_use_session_apartment(tools: DomainTools) -> None:
    context = FakeToolContext("101")

    reservations = tools.list_my_reservations(context)
    visitors = tools.list_my_visitors(context)
    cancelled = tools.cancel_my_reservation("salao-de-festas", "2030-03-16", context)

    assert reservations["reservas"][0]["codigo"] == "RSV-1377"
    assert visitors == {"visitantes": []}
    assert cancelled == {"status": "not_found"}


def test_free_reservation_executes_without_confirmation(tools: DomainTools) -> None:
    context = FakeToolContext("101")

    result = tools.reserve_area("quadra", "2030-04-06", context)

    assert result["status"] == "created"
    assert context.requested is None


def test_paid_reservation_waits_for_route_confirmation(tools: DomainTools) -> None:
    context = FakeToolContext("101")

    pending = tools.reserve_area("salão de festas", "2030-04-20", context)

    assert pending["status"] == "confirmation_required"
    assert context.requested["payload"] == {
        "acao": "reservar_area",
        "detalhes": {
            "area": "salao-de-festas",
            "data": "2030-04-20",
            "taxa": 150.0,
        },
    }
    assert tools.repository.list_reservations("101") == [
        {"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"}
    ]

    denied = tools.reserve_area(
        "salao-de-festas", "2030-04-20", FakeToolContext("101", FakeConfirmation(False))
    )
    assert denied == {"status": "rejected"}
    assert not any(
        item["data"] == "2030-04-20"
        for item in tools.repository.list_reservations("101")
    )

    approved = tools.reserve_area(
        "salao-de-festas", "2030-04-20", FakeToolContext("101", FakeConfirmation(True))
    )
    assert approved["status"] == "created"


def test_visitor_always_requires_route_confirmation(tools: DomainTools) -> None:
    context = FakeToolContext("101")

    pending = tools.authorize_visitor("Joana Ribeiro", "2030-04-21", context)

    assert pending["status"] == "confirmation_required"
    assert context.requested["payload"]["detalhes"] == {
        "nome": "Joana Ribeiro",
        "data": "2030-04-21",
    }
    assert tools.repository.list_visitors("101") == []

    approved = tools.authorize_visitor(
        "Joana Ribeiro", "2030-04-21", FakeToolContext("101", FakeConfirmation(True))
    )
    assert approved == {"status": "authorized"}


def test_unavailable_response_does_not_expose_occupant(tools: DomainTools) -> None:
    result = tools.reserve_area(
        "salao-de-festas", "2030-03-16", FakeToolContext("101", FakeConfirmation(True))
    )

    assert result == {"status": "unavailable"}
    assert "302" not in str(result)
    assert "RSV-4821" not in str(result)


def test_tools_reject_noncanonical_or_impossible_dates(tools: DomainTools) -> None:
    context = FakeToolContext("101")

    assert tools.check_availability("quadra", "2030-4-6") == {"status": "invalid_date"}
    assert tools.reserve_area("quadra", "2030-02-30", context) == {"status": "invalid_date"}
    assert tools.authorize_visitor("Joana", "tomorrow", context) == {"status": "invalid_date"}
