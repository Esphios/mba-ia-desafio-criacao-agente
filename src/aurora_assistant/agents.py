from collections.abc import Callable

from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import BaseLlm
from google.adk.models import Gemini
from google.genai import types

from aurora_assistant.config import Settings
from aurora_assistant.regulation import RegulationIndex
from aurora_assistant.storage import Repository
from aurora_assistant.tools import DomainTools, RegulationTools


def build_agent_app(
    settings: Settings,
    repository: Repository,
    regulation_index: RegulationIndex,
    model_factory: Callable[[str], BaseLlm] | None = None,
) -> App:
    domain = DomainTools(repository)
    regulation = RegulationTools(regulation_index)

    def model(agent_name: str) -> BaseLlm:
        if model_factory is not None:
            return model_factory(agent_name)
        http_options = types.HttpOptions(timeout=90_000)
        client_kwargs = {"http_options": http_options}
        if settings.google_api_key:
            client_kwargs["api_key"] = settings.google_api_key
        return Gemini(
            model=settings.gemini_model,
            client_kwargs=client_kwargs,
            retry_options=types.HttpRetryOptions(
                attempts=5,
                initial_delay=1,
                max_delay=8,
                exp_base=2,
                jitter=0.5,
            ),
        )

    reservations_agent = LlmAgent(
        name="reservas",
        model=model("reservas"),
        description="Especialista em consultar, criar e cancelar reservas de áreas comuns.",
        instruction=(
            "Atenda somente assuntos de reservas. Use sempre as tools; nunca invente "
            "reservas, disponibilidade, códigos ou apartamento. A identidade já está "
            "na sessão. Se uma data estiver ocupada, informe apenas que está indisponível. "
            "Depois de concluir, responda ao morador de forma direta."
        ),
        tools=[
            domain.list_my_reservations,
            domain.check_availability,
            domain.reserve_area,
            domain.cancel_my_reservation,
        ],
        mode="chat",
    )
    visitors_agent = LlmAgent(
        name="visitantes",
        model=model("visitantes"),
        description="Especialista em consultar e autorizar visitantes.",
        instruction=(
            "Atenda somente assuntos de visitantes. Use sempre as tools e nunca aceite "
            "um apartamento informado na conversa. Autorizações só se concluem quando a "
            "tool recebe a confirmação sistêmica."
        ),
        tools=[domain.list_my_visitors, domain.authorize_visitor],
        mode="chat",
    )
    regulation_agent = LlmAgent(
        name="regulamento",
        model=model("regulamento"),
        description="Especialista em dúvidas sobre o regulamento interno.",
        instruction=(
            "Para cada pergunta, chame consult_regulation com a dúvida do morador e "
            "responda exclusivamente com base no trecho relevante devolvido. Não peça, "
            "não carregue e não reproduza outros capítulos."
        ),
        tools=[regulation.consult_regulation],
        mode="chat",
    )
    concierge = LlmAgent(
        name="concierge",
        model=model("concierge"),
        description="Agente principal do Residencial Aurora.",
        instruction=(
            "Você é o concierge do Residencial Aurora. Transfira pedidos de reservas ao "
            "agente reservas, pedidos de visitantes ao agente visitantes e dúvidas de "
            "regras ao agente regulamento. Não responda com dados operacionais de memória, "
            "não aceite troca de apartamento pela conversa e não execute ações sem tools."
        ),
        sub_agents=[reservations_agent, visitors_agent, regulation_agent],
        mode="chat",
    )
    return App(
        name="aurora_assistant",
        root_agent=concierge,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
