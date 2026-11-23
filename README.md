# Getnet Multi-Agent Support

Serviço HTTP em que **três agentes cooperam** para responder mensagens de clientes da Getnet:

| Agente | Papel |
|---|---|
| **Router** | Lê a mensagem e escolhe um fluxo permitido (`knowledge`, `support`, `support_then_knowledge`, `combined`, `clarify`). |
| **Knowledge** | Responde perguntas públicas com **RAG** sobre páginas oficiais da Getnet (Chroma) e **busca web** (Tavily) para perguntas gerais (clima, câmbio). |
| **Customer Support** | Consulta dados **individuais** do cliente com duas ferramentas: `get_receivables` e `get_terminal_diagnostics`. |

Ideia central: **o LLM interpreta e escolhe ferramentas; o código controla identidade, sequência de execução, evidências e limites.** Cada resposta carrega as fontes que a sustentam, e os valores financeiros vêm da ferramenta, não da geração.

**Link da demo:** https://youtu.be/ro0qdh9WJQE
- 182 testes passando;
- 14 de 15 casos de avaliação passando;
- o único caso que falha está documentado como limitação conhecida.

> A especificação de comportamento (critérios de aceite, contratos, falhas) está em [`spec.md`](spec.md); o plano de implementação em `docs/superpowers/plans/`.

## Estado da verificação

| Verificado | Como |
|---|---|
| Lógica dos agentes, orquestração, API, identidade, falhas, RAG e ingestão | 182 testes (`pytest`), rodando também dentro do Docker; inclui o `chat.completions.parse` **real** do SDK da OpenAI (só a rede é simulada) com todas as ferramentas em modo estrito |
| Fluxo Docker ponta a ponta, com serviços reais | `docker compose build` → `run --rm api python -m app.ingest` (**25 páginas, 41 chunks, 0 falhas**) → `up`; `/health` com `index: ready`; perguntas de recebíveis, Pix e produto inexistente respondidas |
| Serviços reais (OpenAI `gpt-6-luna` com `OPENAI_REASONING_EFFORT=none`, embeddings, Tavily) | `pytest -m live`: **7/7** (cada pergunta de RAG recupera a página esperada; Tavily responde). `evals`: **14/15** contra a API do Docker (veja [Avaliação](#avaliação-de-ia)) |

Verificação feita em 10/11/2026. Os testes padrão não usam rede nem chaves; as verificações com serviços reais ficam marcadas `@pytest.mark.live`.

## Como executar

Pré-requisitos: Docker (ou Python 3.11), uma chave OpenAI e uma chave Tavily.

```bash
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
                              # preencha OPENAI_API_KEY, TAVILY_API_KEY e OPENAI_CHAT_MODEL
docker compose build
docker compose run --rm api python -m app.ingest   # baixa as páginas oficiais e cria o índice (volume chroma_data)
docker compose up                                   # API em http://localhost:8000
```

Em seguida, abra `http://localhost:8000/docs` (Swagger) ou use os exemplos abaixo. Para parar: `Ctrl+C` (ou `docker compose down`; o índice permanece no volume).

**Porta 8000 ocupada?** A porta do host é configurável:
```bash
API_PORT=8011 docker compose up            # PowerShell: $env:API_PORT=8011; docker compose up
```

**Ingestão falha com `permission denied` ou o índice parece corrompido?** Provavelmente há um volume antigo de outra versão da imagem. Recrie-o: `docker compose down -v` e rode a ingestão de novo.

| Variável | Para quê |
|---|---|
| `OPENAI_API_KEY` | chat com tool calling e embeddings (obrigatória) |
| `OPENAI_CHAT_MODEL` | modelo de chat com tool calling e saída estruturada (obrigatória) |
| `OPENAI_REASONING_EFFORT` | opcional. Modelos de raciocínio (ex.: `gpt-6-luna`) só aceitam ferramentas no Chat Completions com `none`; deixe vazio para modelos que rejeitam o parâmetro (ex.: `gpt-4o`) |
| `OPENAI_EMBEDDING_MODEL` | padrão `text-embedding-3-small` |
| `TAVILY_API_KEY` | busca web; sem ela o Knowledge responde só com RAG e informa a lacuna em perguntas gerais |
| `CHROMA_PATH` | onde o índice é persistido (no Docker: `/data/chroma`, volume nomeado) |

A imagem da API roda como usuário sem privilégios e tem `HEALTHCHECK` em `/health`. A API recusa iniciar, com mensagem clara, se `OPENAI_API_KEY` ou `OPENAI_CHAT_MODEL` faltarem. Se o índice estiver ausente, `GET /health` mostra `"index": "missing"` e perguntas que exigem RAG devolvem 503 `index_unavailable`.

### Exemplo

`/chat` exige um token. Na demo (`APP_ENV=development`, padrão do `docker compose`), o emissor local entrega tokens para os principais de desenvolvimento:

```bash
TOKEN=$(curl -s localhost:8000/dev/token -H 'content-type: application/json' -d '{"sub": "user-a"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -s localhost:8000/chat -H 'content-type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"message": "When will the money from yesterday'\''s sales be deposited?", "user_id": "cliente1988"}'
```

```jsonc
{
  "request_id": "req_ab12cd34ef56",
  "status": "answered",            // answered | partial | needs_clarification | insufficient_evidence | handoff_recommended | customer_not_found
  "answer": "Recebíveis das vendas de 06/11/2026 (dados demonstrativos):\n- Venda R-1988-001: R$ 1.250,00, liquidação em 07/11/2026, status: previsto ...",
  "agents_used": ["router", "customer_support"],
  "sources": [{"id": "R-1988-001", "type": "customer_record", "title": "Recebível R-1988-001", "url": null}],
  "data_mode": "demo",             // "demo" quando dados sintéticos de cliente foram usados
  "workflow": "support",           // campos extras, úteis para depuração e avaliação
  "tools_used": ["get_receivables"]
}
```

Clientes sintéticos: `cliente1988` (recebíveis previstos + terminal offline com `ADQ 1-19` e última transação recusada), `cliente2000` (outros recebíveis + 2 terminais funcionando). Principais de desenvolvimento: `user-a` → `cliente1988`, `user-b` → `cliente2000`, `user-c` → `desconhecido` (autorizado, sem cadastro → `customer_not_found` nas perguntas de dados individuais) e `operator-1` (operador, sem estabelecimento). `user_id` é só um seletor: precisa ser um estabelecimento do token, senão 403.

### Erros HTTP

| Código | Quando |
|---|---|
| 401 `unauthorized` | token ausente, inválido, expirado, de outro emissor/audiência ou sem `tenant_id` — o LLM nem é chamado |
| 403 `forbidden` | `user_id` que não pertence ao principal (mesma resposta para cliente existente ou não) — o LLM nem é chamado |
| 403 `insufficient_scope` | o Router escolheu o Support, mas o token não tem escopo de dados do cliente |
| 400 `customer_selection_required` | principal com mais de um estabelecimento sem `user_id` |
| 404 `not_found` | `conversation_id` inexistente, de outro principal/cliente ou expirada |
| 409 | `conversation_busy` (outro turno da conversa em andamento), `idempotency_key_reused` (mesma chave, outro conteúdo), `request_in_progress`, `conversation_conflict` |
| 422 | payload inválido (mensagem vazia/gigante, `user_id` fora de `^[A-Za-z0-9_-]{1,64}$`) — o LLM nem é chamado |
| 503 `llm_unavailable` | provedor de LLM falhou de forma persistente (1 retry com pausa) ou devolveu saída fora do schema duas vezes |
| 503 `index_unavailable` | índice ausente/incompatível numa pergunta que exige RAG |
| 500 `internal_error` | erro inesperado (JSON, sem detalhes internos; o tipo do erro vai para o log) |
| 503 `budget_exceeded` | limite de chamadas (LLM/ferramentas) esgotado **sem nenhum resultado parcial** |
| 503 `overloaded` | mais `/chat` simultâneos do que `MAX_CONCURRENT_CHATS` no processo; resposta imediata com `Retry-After: 2` |
| 504 `deadline_exceeded` | prazo (40 s) esgotado **sem nenhum resultado parcial** — com resultado parcial a resposta é `partial` |

### Desenvolvimento local e testes

```bash
python -m venv .venv && .venv/Scripts/activate        # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m ruff check . && python -m ruff format --check . && python -m mypy app && python -m pytest   # verificação única
python -m pytest -m live                              # verificações reais (exigem .env)
docker compose --profile test run --rm tests          # a mesma suíte, no Docker
python scripts/smoke.py --base-url http://localhost:8000   # smoke contra um container no ar (sem LLM)
```

## Arquitetura

```mermaid
flowchart TD
    A["POST /chat"] --> B["Validação (Pydantic) + RequestContext"]
    B --> R["Router Agent (plano estruturado)"]
    R --> V["validate_plan (fluxos permitidos)"]
    V -->|knowledge| K["Knowledge Agent"]
    V -->|support| S["Customer Support Agent"]
    V -->|support_then_knowledge| S
    S -->|"contexto técnico mínimo"| K
    V -->|combined| S
    V -->|combined| K
    V -->|clarify| Q["Pedido de esclarecimento"]
    K --> D["RAG (Chroma)"]
    K --> W["Busca web (Tavily)"]
    S --> T["get_receivables / get_terminal_diagnostics"]
    S --> C["Composição determinística + status"]
    K --> C
    Q --> C
    C --> J["JSON: resposta, status, fontes"]
```

### Fluxo de mensagens

1. `POST /chat` valida o token (RS256, emissor, audiência, validade) e monta o `Principal`; confronta o `user_id` com os estabelecimentos do principal (resolvidos no servidor) e só então cria um `RequestContext` por requisição (cliente autorizado, escopos, relógio, orçamento, evidências). Não existe `current_user_id` global.
2. O **Router** faz uma chamada estruturada (sem ferramentas e sem a identidade do cliente). O **código** valida o plano: fluxo permitido, ordem e agentes corretos, no máximo 2 tarefas. Plano inválido → 1 nova tentativa → `clarify`.
3. O orquestrador executa o fluxo **em sequência**. Agentes não se chamam: o código passa objetos tipados.
   - `support_then_knowledge`: o Support consulta o diagnóstico; só `terminal_model`, `error_code` e `issue` seguem ao Knowledge (nunca ids, valores ou identidade).
   - `combined`: duas subperguntas independentes; cada ramo é isolado — se um falha, a parte válida sai com `status="partial"` e uma nota de pendência.
4. A composição é determinística (seções por especialista). O status final segue regras explícitas (`merge_status`).

### Como as ferramentas (tool calling) são usadas

- Cada agente roda um loop (`run_agent`): o LLM chama ferramentas até devolver uma **saída estruturada** (`AgentOutput`).
- Ferramentas são `ToolSpec` com `args_model` Pydantic **`extra="forbid"`**, enviadas ao OpenAI em modo **strict** (`pydantic_function_tool`): o LLM só escolhe o que a ferramenta declara, e campos opcionais vão como `null` explícito. A identidade do cliente **não é argumento** — vem de `ctx.user_id`. Um tool call com `user_id` extra é rejeitado antes de executar.
- Nome desconhecido, argumentos inválidos ou orçamento de ferramentas esgotado voltam ao LLM como resultado de erro, sem derrubar a requisição. Se as rodadas de ferramentas acabam, uma chamada final **sem ferramentas** força a resposta com o que já foi coletado.
- Toda ferramenta devolve `ToolResult` com `status`, dados tipados e **evidências** (`id`, `type`, `title`, `url`). O servidor registra as evidências da requisição; o LLM só **seleciona ids**, e URLs/títulos finais vêm dos metadados. Ids inventados são descartados.

### Pipeline de RAG

1. **Ingestão** (`python -m app.ingest`, comando separado da API): lê `data/sources.json` → baixa cada URL (timeout, falhas registradas) → extrai o conteúdo principal (trafilatura, sem menu/rodapé) → *chunking* (~700 tokens estimados, sobreposição de ~80, perguntas de FAQ ficam junto da resposta) → embeddings OpenAI → persiste no Chroma. Repetível: reingerir substitui os chunks da URL e **remove do índice as páginas retiradas de `sources.json`** (uma página que apenas falhou nesta rodada mantém os chunks antigos). Sem `OPENAI_API_KEY` sai com código 2 antes de baixar qualquer página. Trocar `OPENAI_EMBEDDING_MODEL` exige índice novo: `docker compose down -v` (apaga o volume) e ingerir de novo. Imprime resumo (páginas ok, falhas, chunks); sai com erro só se **nenhuma** página entrar.
2. **Armazenamento**: Chroma persistente (volume Docker). Cada chunk guarda `chunk_id`, `source_url`, `title`, `market`, `language`, `fetched_at`, `content_hash`. Um `manifest.json` registra modelo e dimensão dos embeddings; consultar com outro modelo é recusado (`incompatible`).
3. **Recuperação**: `retrieve_getnet_knowledge(query, market, topic)` → `top_k=5`, filtrando por mercado. A distância vetorial **não** é exposta como "confiança".
4. **Geração**: o Knowledge responde só com o que os trechos sustentam, preserva condições e limitações, cita ids de evidência e declara a lacuna quando não há suporte (`insufficient_evidence`). Conteúdo recuperado é tratado como **dado**, nunca como instrução.

**Fontes** (`data/sources.json`, 25 páginas): FAQ oficial, páginas de produto (Get Clássica, aluguel/planos, Get Smart, Get Lite), Pix, Get Conta, antecipação, erros de maquininha (ADQ 1-19), Wi-Fi, status de vendas, crediário, Link de Pagamento/WhatsApp e a página global sugerida no enunciado (`getnet.net/en`, mercado `global`). Todas foram sondadas com HTTP real e conteúdo extraído ≥ 500 caracteres.
Notas: o enunciado sugere o site global, mas os cenários são brasileiros, então o corpus é majoritariamente `site.getnet.com.br`. A página `/maquininha/get-smart/` é renderizada por JavaScript (quase sem texto extraível), então informações da Get Smart vêm de outras páginas (FAQ, soluções Get Smart, planos). Páginas coletadas numa data podem trazer ofertas antigas — por isso o prompt manda avisar que condições podem mudar.

## Decisões e trade-offs

- **Sem LangGraph**: fluxo pequeno e explícito em funções async é mais fácil de depurar e explicar; a arquitetura lógica valeria igual com um grafo.
- **Composição e status em código**: evita uma chamada extra de LLM. O bloco de recebíveis é um template com os dados da ferramenta, e o texto livre do LLM é trocado por uma nota fixa se afirmar pagamento/confirmação de um recebimento que a ferramenta não marca como `pago` (verificação por padrões, em PT e EN: é uma salvaguarda forte, não uma prova semântica).
- **Execução sequencial**: simples e previsível; paralelizar o `combined` é uma otimização futura.
- **Chroma local**: zero serviços extras para a demo; para múltiplas instâncias, trocar por um vetor store gerenciado (a interface `KnowledgeIndex` isola isso).
- **Identidade pelo token, acesso pelo servidor**: o JWT diz quem chama; o mapa principal → estabelecimentos é do servidor, e o `user_id` do payload é só um seletor conferido contra ele. Em desenvolvimento um emissor local (chave efêmera) assina tokens pelo mesmo caminho criptográfico; produção exige um provedor de identidade (JWKS) e recusa o emissor local. Sem sistema próprio de senhas.
- **Conversa em SQLite**: uma instância basta ao escopo, sem servidor de banco. O turno grava a intenção, executa sem transação aberta e grava o resultado conferindo a versão; a escolha "o segundo" é resolvida pelo servidor contra a lista que ele mesmo apresentou, nunca pelo LLM.
- **Estimativa de tokens (~4 caracteres/token)** no *chunking*, para não depender de baixar um tokenizador.
- Fixtures de clientes usam **offsets de data** para que "ontem" funcione em qualquer dia da demo.

## Guardrails e confiabilidade

- Validação de entrada antes de qualquer chamada ao LLM; identidade só no `RequestContext`; o repositório filtra por esse contexto e terminais de outro cliente nunca são revelados.
- Plano do Router validado pelo código; limites por requisição configuráveis (≤2 especialistas, ≤10 chamadas LLM incluindo retry, verificação e replanejamento, ≤5 ferramentas, ≤2 buscas web, deadline 40 s).
- **Replanejamento limitado** (`app/replanning.py`): depois de cada etapa o código observa o resultado e pode mudar o plano uma vez: reparar as lacunas apontadas pelo verificador (`knowledge_repair`) ou acrescentar a orientação oficial para um código de erro visto no diagnóstico (`guidance_added`). Só começa se sobrar orçamento para concluir e verificar; sem evidência nova, mantém o que já foi comprovado (`knowledge_repair:no_progress`). A resposta lista as mudanças em `plan_changes`.
- Evidências verificadas pelo servidor; sem evidência validada → `insufficient_evidence` **e o texto do LLM é descartado** (resposta fixa "não encontrei nas fontes"), para não vazar condição comercial vinda da memória do modelo.
- Falhas de um ramo viram `partial`. `handoff_recommended` é emitido quando há **problema técnico diagnosticado e nenhuma orientação oficial** (`support_then_knowledge`). O pedido explícito de "falar com um atendente" também abre o caso (fluxo `handoff`). Pelo `/chat`, o turno abre um **caso interno real** (fila, operador, decisão auditável) e devolve `handoff_id` com `run_status=awaiting_operator`; depois da decisão, um worker entrega a resposta do atendente na própria conversa, identificada como resposta humana. Terminal ambíguo vira `needs_clarification`.
- Busca web recebe só consulta pública (sem dados do cliente); logs não contêm mensagem, resposta nem `user_id`.

## Estratégia de testes

Pirâmide pensada para um sistema multiagente, onde o LLM é a parte não determinística:

1. **Integração da API até a resposta**, com um LLM *roteirizado* (`ScriptedLLM`) e fakes de embeddings/busca/relógio. Verifica contratos, roteamento, ferramentas, isolamento entre clientes, fluxo composto, falhas e limites **sem custo e sem flakiness**. Cada critério de aceite da spec tem um teste (`tests/test_api.py`, `tests/test_orchestrator.py`).
2. **Unidade das peças determinísticas**: chunking, índice, ferramentas, validação de plano, template de recebíveis, cliente OpenAI (com stub).
3. **Verificações reais separadas** (`-m live`, exigem `.env`): ingestão, embeddings, recuperação, Tavily.
4. **Avaliação de IA** (abaixo) para o que testes determinísticos não alcançam: qualidade de roteamento e de resposta com o modelo real.

Para ampliar a integração: gravar/reproduzir respostas reais do provedor (VCR) para regressão barata, e testes de carga para validar os limites de orçamento e deadline sob concorrência (já há um teste de concorrência com usuários distintos).

## Avaliação de IA

`evals/cases.json` traz **15 casos**: as 10 mensagens do enunciado + composta, terminal ambíguo, produto inexistente (sem evidência), cliente desconhecido e tentativa de troca de usuário. Cada caso define rota, ferramentas obrigatórias e permitidas (qualquer outra reprova), fontes aceitáveis, status, valores ligados ao seu status, valores monetários permitidos, âncoras semânticas e afirmações proibidas; listas vazias ficam fora do denominador e erros de requisição contam como falha. Cada execução grava um relatório JSON por caso em `evals/reports/`.

```bash
docker compose up -d
python -m evals.run_evals            # imprime PASS/FAIL por caso e as métricas
```

Métricas: acerto de rota, de ferramentas, de fontes esperadas, de status, preservação de valores e ausência de afirmações proibidas. A pontuação detecta regressões objetivas; a **qualidade semântica** das respostas continua exigindo revisão manual (evolução natural: LLM-as-judge com rubrica). 

**Resultado mais recente (10/11/2026, API no Docker, `gpt-6-luna`): 14/15 casos.**

| Métrica | Acertos |
|---|---|
| rota | 15/15 |
| ferramentas | 15/15 |
| fontes esperadas | 6/6 |
| status | 13/14 |
| preservação de valores | 15/15 |
| afirmações proibidas ausentes | 15/15 |

O caso que falha é o `x13` (produto inexistente, "Getnet Quantum 9000"): o texto diz corretamente que **não encontrou** a taxa, mas o status sai `answered` em vez de `insufficient_evidence`, porque o modelo cita trechos genéricos (que mencionam que as taxas variam) como evidência. É um limite conhecido: o status depende de o modelo não citar evidência quando conclui que não achou, e isso não é imposto em código. Nota: na primeira rodada o `e04` (Pix) também falhou, mas era uma lacuna do dataset — a resposta citava a FAQ oficial (`/duvidas/`), que faltava na lista de fontes esperadas; a lista foi corrigida depois de conferir a fonte citada.

## Observabilidade e monitoramento em produção

Implementado: logs JSON estruturados com **allowlist de campos** (campo desconhecido é descartado; ids de recebíveis e diagnósticos não são logados, pois embutem o número do cliente), uma linha por evento (`route`, `tool_call`, `agent_completed`, `agent_failed`, `replan`, `public_query_refused`, `overloaded`, `request_completed`, `request_failed`, `shutdown`) com `request_id`, `workflow`, `agent`, `tool_name`, `status`, `duration_ms`, `source_ids`, `index_version`, consumo agregado (`llm_calls`, `tool_calls`, `web_searches`, `embedding_calls`, `token_usage`) e `error_code`. `GET /health` (liveness) e `GET /ready` (índice, estado e ciclo de vida, 503 quando não pronto). Consultas que saem para busca web ou embeddings passam por uma checagem de dados do cliente (`app/privacy.py`). O CI roda a suíte dentro da imagem e faz smoke e SIGTERM no container da API construído.

Descrito (não implementado): painéis de latência p50/p95 por fluxo e por agente, taxa de `partial`/`insufficient_evidence`/`customer_not_found`, custo por requisição (tokens), taxa de 503/504; traces distribuídos (OpenTelemetry) com um span por agente/ferramenta; alertas em picos de `llm_unavailable`, queda de `index: ready`, aumento de `insufficient_evidence` (sinal de índice defasado) e do deadline; reexecução periódica dos `evals` como teste de regressão do modelo e dos prompts.

## Como estender

- **Nova ferramenta**: criar o `args_model` (`extra="forbid"`, sem identidade) e a função `async (args, ctx) -> ToolResult`, registrar como `ToolSpec` em `build_customer_tools` ou `build_knowledge_tools`, e acrescentar teste em `tests/`.
- **Novo fluxo**: adicionar o valor em `Workflow` (`app/schemas.py`), a regra em `validate_plan` (`app/agents/router.py`), a execução em `_run_workflow` (`app/orchestrator.py`), a descrição no `ROUTER_SYSTEM` (`app/prompts.py`) e um caso em `evals/cases.json`.
- **Nova fonte de conhecimento**: acrescentar a URL em `data/sources.json` e rodar a ingestão.
- **Dados reais de clientes**: implementar `CustomerRepository` (`app/repositories.py`) e injetar em `build_deps` (`app/main.py`).

## Custo de desenvolvimento (tokens e dinheiro)

Todo o código, os testes, a documentação e as revisões foram feitos com **uma única sessão do Claude Code usando Claude Sonnet 5.5** (`claude-sonnet-5-5`), mais **2 subagentes também Sonnet 5.5** (revisão independente da spec e revisão final do código) e o Claude Haiku 5.5 apenas para 2 buscas web. Números medidos a partir do log local da sessão (uso por mensagem), no momento em que esta seção foi escrita:

| Item | Tokens | Custo (US$) |
|---|---:|---:|
| Entrada não cacheada | 394 | 0,00 |
| Saída (inclui raciocínio) | 358.148 | 3,58 |
| Escrita de cache, 5 min | 197.738 | 0,49 |
| Escrita de cache, 1 h | 1.350.276 | 5,40 |
| Leitura de cache | 47.407.492 | 4,74 |
| **Total Sonnet 5.5** (176 chamadas ao modelo) | **49.314.048** | **14,22** |
| Haiku 5.5 (2 buscas web) | 38.337 | ≈ 0,02 |
| **Total** | **≈ 49,4 milhões** | **≈ US$ 14,25** |

- Do total, ≈ **96% dos tokens processados foram leituras de cache** (a conversa é reenviada a cada passo e quase sempre vem do cache); os tokens efetivamente "novos" (entrada + saída + escrita de cache) somam ≈ 1,9 milhão, dos quais ≈ **358 mil de saída**.
- A sessão principal custou ≈ US$ 13,21; os dois subagentes de revisão, ≈ US$ 1,01.
- **Tarifas usadas** (por milhão de tokens): entrada US$ 2,00, saída US$ 10,00, leitura de cache US$ 0,10, escrita de cache de 5 min US$ 2,50 e de 1 h US$ 4,00. Essas tarifas são as que reproduzem exatamente os três checkpoints de custo que o próprio Claude Code gravou na sessão (US$ 0,58, 7,23 e 9,57). O último checkpoint registrado pelo Claude Code foi **US$ 9,60**; o total acima inclui o trabalho posterior a ele. A tabela pública de preços consultada lista a leitura de cache do Sonnet 5.5 a US$ 0,20/M; com essa tarifa o total subiria para ≈ US$ 19.
- **Não estão incluídos:** o custo de *executar* a aplicação (chamadas à OpenAI e ao Tavily durante os testes, `evals` e demonstrações), e o tempo de desenvolvimento, que **não foi cronometrado**.

## Limitações conhecidas

- Dados de clientes são sintéticos. O mapa de acesso (principal → estabelecimentos) é uma fixture; em produção seria outra implementação de `AccessRepository` ligada ao cadastro real.
- A verificação de suporte e cobertura do Knowledge é uma chamada de LLM: é calibrada pelo prompt e varia entre execuções. Ela é conservadora (prefere `partial`/`insufficient_evidence` a liberar texto sem suporte); em duas rodadas seguidas dos `evals` após a revisão v2, o placar foi 10/15 e 12/15, sem nenhuma resposta errada liberada (rota, ferramentas, valores e afirmações proibidas em 100%) e com o `x13` corrigido nas duas; as falhas restantes são `partial`/`insufficient_evidence` (comparação Get Clássica × Get Smart, Pix, crediário, combinado) e uma cotação web com snippet ambíguo.
- O texto do Suporte pode sair em português mesmo com pergunta em inglês (o bloco de recebíveis é um template em português); o Conhecimento responde no idioma da pergunta.
- O filtro que impede o LLM de afirmar "pago/confirmado" é por padrões (pt/en) e é conservador: pode trocar frases inofensivas por uma nota fixa.
- Os itens de "Pendências" vêm do próprio modelo e podem terminar com ponto duplicado.
- Páginas de produto renderizadas por JavaScript não são extraídas (sem navegador headless).
- Valores de limites (deadline, orçamentos, `top_k`, tamanho de chunk) são pontos de partida de engenharia, não medidos.
