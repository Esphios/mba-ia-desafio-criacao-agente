# Assistente do Residencial Aurora

API conversacional construída com Google ADK para reservas de áreas comuns, cancelamentos, autorização de visitantes e dúvidas sobre o regulamento. O modelo conduz a conversa; as regras que protegem cobrança, acesso, identidade, persistência e concorrência são aplicadas por código e pelo banco.

## Arquitetura

A aplicação usa Python 3.12, FastAPI, Google ADK 2.15.1 e dois arquivos SQLite persistentes derivados de `AURORA_DATABASE_PATH`: o caminho configurado guarda o domínio e o sufixo `-adk` guarda sessões/eventos. A separação impede deadlock entre a transação do `Runner` e a gravação concorrente de reservas.

- `concierge`: agente principal. Identifica o domínio do pedido e transfere a conversa; não possui tools operacionais nem recebe o regulamento em suas instruções.
- `reservas`: consulta, cria e cancela reservas por meio de tools. Reservas pagas interrompem a execução para confirmação sistêmica.
- `visitantes`: consulta visitantes e cria autorizações somente após confirmação sistêmica.
- `regulamento`: chama uma busca local que devolve somente o capítulo relacionado à pergunta.

A composição está em [`src/aurora_assistant/agents.py`](src/aurora_assistant/agents.py), na função `build_agent_app`. As transferências nativas mantêm a autoria do especialista nos eventos, permitindo que o `Runner` retome a confirmação no agente que a solicitou.

Fluxo de uma mensagem:

1. A API resolve `session_id` no registro persistente e recupera o apartamento autenticado.
2. O `Runner` executa o concierge e o especialista selecionado.
3. Tools leem o apartamento do `ToolContext`; nenhum argumento do modelo escolhe a identidade.
4. Quando há confirmação, a API devolve a chamada `adk_request_confirmation` como pendência sem executar o efeito.
5. A rota de confirmações envia um `FunctionResponse` com o mesmo ID e invocation ID para retomar a execução.

## Garantias

### 1. Cobrança ou acesso só com confirmação

- [`DomainTools.reserve_area` e `DomainTools.authorize_visitor`](src/aurora_assistant/tools.py) verificam `tool_context.tool_confirmation` e chamam `request_confirmation` antes de qualquer gravação.
- [`AgentRuntime.respond_confirmation`](src/aurora_assistant/runtime.py) aceita somente IDs pendentes na própria sessão e retoma o ADK com o ID e invocation ID originais.
- [`Repository.accept_confirmation`](src/aurora_assistant/storage.py) impede replay sequencial; IDs inexistentes, de outra sessão ou já consumidos resultam em HTTP 409.
- O efeito usa sessão, invocation ID e `function_call_id` original como chave idempotente, validando também ação e argumentos. Se o processo cair durante a retomada, o estado durável `accepted/completed` reconcilia o efeito sem repetir reserva ou visitante.
- Quadra com taxa zero e cancelamento próprio seguem diretamente, sem pendência.

### 2. Cada sessão pertence a um apartamento

- [`Repository.create_session`](src/aurora_assistant/storage.py) grava o vínculo imutável `session_id → apartamento`.
- As tools particulares em [`src/aurora_assistant/tools.py`](src/aurora_assistant/tools.py) não aceitam apartamento: `_apartment` lê exclusivamente `ToolContext.state`.
- `check_availability` e conflitos retornam somente livre/ocupada, sem código ou apartamento do ocupante.
- Datas são validadas e normalizadas como calendário ISO estrito (`YYYY-MM-DD`) antes de qualquer consulta ou gravação.

### 3. Nada se perde no reinício

- [`AgentRuntime`](src/aurora_assistant/runtime.py) usa `DatabaseSessionService` no arquivo SQLite com sufixo `-adk` para sessões e eventos.
- [`Repository`](src/aurora_assistant/storage.py) persiste vínculo de sessão, reservas, cancelamentos, visitantes e confirmações no arquivo configurado.
- Reservas canceladas permanecem no histórico, e novos códigos usam UUID prefixado por `RSV-`, portanto códigos antigos não são reutilizados.

### 4. O regulamento é consultado, não carregado

- [`RegulationIndex`](src/aurora_assistant/regulation.py) divide `dados/regulamento.md` por capítulo e devolve somente o melhor capítulo para a consulta.
- O concierge não recebe o regulamento; apenas o especialista possui a tool `consult_regulation`.
- Como a tool retorna um capítulo, os eventos não carregam capítulos não relacionados.

### 5. Dois moradores, uma reserva

- O schema em [`src/aurora_assistant/storage.py`](src/aurora_assistant/storage.py) cria o índice único parcial `ux_aurora_active_area_date` para `(area, reservation_date)` em reservas ativas.
- `Repository.create_reservation` usa `BEGIN IMMEDIATE`; o índice arbitra no instante da gravação.
- A violação é convertida em `{"status":"unavailable"}`, permitindo HTTP 200 para vencedor e perdedor sem duplicidade.

## Como rodar

### Pré-requisitos

- Python 3.12 ou superior.
- [uv](https://docs.astral.sh/uv/).
- Chave do Google AI Studio com acesso ao modelo configurado.

### Instalação

Na raiz do repositório:

```powershell
Copy-Item .env.example .env
uv sync
```

Preencha `.env`:

```dotenv
GOOGLE_API_KEY=sua-chave
GEMINI_MODEL=gemini-3.8-flash
AURORA_DATABASE_PATH=.runtime/aurora.db
```

`GEMINI_MODEL` pode ser trocado por um modelo estável disponível no seu projeto do Google AI Studio. Não use um alias flutuante para a avaliação.

### Restaurar os dados

O comando apaga os dois bancos de runtime, inclusive sessões, e recria o domínio a partir dos arquivos imutáveis de `dados/`; o banco ADK é recriado na próxima inicialização:

```powershell
uv run aurora-reset
```

### Subir a API

```powershell
uv run aurora-api
```

A API responde em `http://localhost:8000`. A documentação OpenAPI fica em `http://localhost:8000/docs`.

Rotas:

- `POST /sessoes`
- `POST /sessoes/{session_id}/mensagens`
- `POST /sessoes/{session_id}/confirmacoes`
- `GET /sessoes/{session_id}/eventos`
- `GET /apartamentos/{apartamento}/reservas`
- `GET /apartamentos/{apartamento}/visitantes`

### Testes

Testes determinísticos, sem consumir a API Gemini:

```powershell
uv run pytest
```

Fluxo completo dos 15 passos, com chave real e servidor gerenciado pelo script:

```powershell
uv run python scripts/evaluate_flow.py
```

O avaliador usa banco e porta temporários, restaura os dados, inicia a API, executa isolamento, cancelamento, reservas gratuitas e pagas, confirmação, replay, visitante, regulamento, reinício e disputa concorrente, e encerra somente o processo que ele iniciou.
