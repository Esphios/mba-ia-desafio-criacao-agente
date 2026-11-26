from __future__ import annotations

from google.adk.tools import ToolContext

from aurora_assistant.regulation import RegulationIndex
from aurora_assistant.storage import Repository, normalize_date


def _apartment(tool_context: ToolContext) -> str:
    apartment = tool_context.state.get("apartamento")
    if not apartment:
        raise ValueError("Sessão sem apartamento autenticado")
    return str(apartment)


def _date_or_none(value: str) -> str | None:
    try:
        return normalize_date(value)
    except ValueError:
        return None


def _action_key(tool_context: ToolContext) -> str | None:
    call_id = getattr(tool_context, "function_call_id", None)
    if not call_id:
        return None
    session = getattr(tool_context, "session", None)
    session_id = getattr(session, "id", "unit")
    invocation_id = getattr(tool_context, "invocation_id", "unit")
    return f"{session_id}:{invocation_id}:{call_id}"


class DomainTools:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def list_my_reservations(self, tool_context: ToolContext) -> dict:
        """Lista somente as reservas ativas do apartamento autenticado."""
        return {"reservas": self.repository.list_reservations(_apartment(tool_context))}

    def list_my_visitors(self, tool_context: ToolContext) -> dict:
        """Lista somente os visitantes do apartamento autenticado."""
        return {"visitantes": self.repository.list_visitors(_apartment(tool_context))}

    def check_availability(self, area: str, date: str) -> dict:
        """Informa apenas se a área está livre na data, sem revelar o ocupante."""
        date = _date_or_none(date)
        if date is None:
            return {"status": "invalid_date"}
        canonical_area = self.repository.resolve_area(area)
        if canonical_area is None:
            return {"status": "invalid_area"}
        return {
            "area": canonical_area,
            "data": date,
            "disponivel": self.repository.is_available(canonical_area, date),
        }

    def reserve_area(self, area: str, date: str, tool_context: ToolContext) -> dict:
        """Reserva uma área para a sessão; áreas pagas exigem confirmação sistêmica."""
        date = _date_or_none(date)
        if date is None:
            return {"status": "invalid_date"}
        canonical_area = self.repository.resolve_area(area)
        if canonical_area is None:
            return {"status": "invalid_area"}
        area = canonical_area
        apartment = _apartment(tool_context)
        fee = self.repository.area_fee(area)
        if fee is None:
            return {"status": "invalid_area"}

        if fee > 0:
            confirmation = tool_context.tool_confirmation
            if confirmation is None:
                details = {"area": area, "data": date, "taxa": fee}
                tool_context.request_confirmation(
                    hint=f"Confirmar reserva de {area} em {date} com taxa de R$ {fee:.2f}?",
                    payload={"acao": "reservar_area", "detalhes": details},
                )
                return {"status": "confirmation_required", "detalhes": details}
            if not confirmation.confirmed:
                return {"status": "rejected"}

        result = self.repository.create_reservation(
            apartment,
            area,
            date,
            action_key=_action_key(tool_context),
        )
        if not result.ok:
            return {"status": result.reason}
        return {"status": "created", "codigo": result.code, "area": area, "data": date}

    def cancel_my_reservation(
        self, area: str, date: str, tool_context: ToolContext
    ) -> dict:
        """Cancela sem confirmação uma reserva do apartamento autenticado."""
        date = _date_or_none(date)
        if date is None:
            return {"status": "invalid_date"}
        canonical_area = self.repository.resolve_area(area)
        if canonical_area is None:
            return {"status": "invalid_area"}
        result = self.repository.cancel_reservation(
            _apartment(tool_context), canonical_area, date
        )
        return {"status": "cancelled" if result.ok else "not_found"}

    def authorize_visitor(
        self, name: str, date: str, tool_context: ToolContext
    ) -> dict:
        """Autoriza visitante somente após confirmação sistêmica."""
        date = _date_or_none(date)
        if date is None:
            return {"status": "invalid_date"}
        apartment = _apartment(tool_context)
        confirmation = tool_context.tool_confirmation
        if confirmation is None:
            details = {"nome": name, "data": date}
            tool_context.request_confirmation(
                hint=f"Confirmar autorização de entrada para {name} em {date}?",
                payload={"acao": "autorizar_visitante", "detalhes": details},
            )
            return {"status": "confirmation_required", "detalhes": details}
        if not confirmation.confirmed:
            return {"status": "rejected"}

        self.repository.add_visitor(
            apartment,
            name,
            date,
            action_key=_action_key(tool_context),
        )
        return {"status": "authorized"}

    def reconcile_confirmed_action(
        self,
        apartment: str,
        action: str,
        details: dict,
        action_key: str,
    ) -> dict:
        """Reconcile an accepted ADK action after an interrupted resume.

        This method is application-only and is not exposed in any agent's tool
        list. The apartment comes from the immutable session binding.
        """
        if action == "reservar_area":
            result = self.repository.create_reservation(
                apartment,
                str(details["area"]),
                str(details["data"]),
                action_key=action_key,
            )
            return {
                "status": "created" if result.ok else result.reason,
                "codigo": result.code,
            }
        if action == "autorizar_visitante":
            self.repository.add_visitor(
                apartment,
                str(details["nome"]),
                str(details["data"]),
                action_key=action_key,
            )
            return {"status": "authorized"}
        return {"status": "invalid_action"}


class RegulationTools:
    def __init__(self, index: RegulationIndex) -> None:
        self.index = index

    def consult_regulation(self, query: str) -> dict:
        """Consulta somente o capítulo mais relacionado à pergunta."""
        return {"consulta": query, "trecho_relevante": self.index.search(query)}
