"""Ferramentas do Knowledge Agent. URLs e títulos das evidências vêm dos metadados do índice,
nunca de texto produzido pelo LLM."""

import asyncio
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context import BudgetExceeded, RequestContext
from app.llm import ToolSpec
from app.observability import log_event
from app.privacy import PrivateDataInQuery, check_public_query
from app.rag import KnowledgeIndex
from app.schemas import Evidence, ToolResult
from app.tools.search import SearchProvider, SearchUnavailable

TOP_K = 5
SEARCH_TIMEOUT_SECONDS = 10.0


def _refuse(tool: str, ctx: RequestContext, exc: PrivateDataInQuery) -> ToolResult:
    log_event("public_query_refused", request_id=ctx.request_id, tool=tool, reason=exc.reason)
    return ToolResult(
        status="error",
        error="query refused: it contains customer data; send only a public query",
    )


class RetrieveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    # default_factory: o modo estrito do LLM não aceita a palavra-chave `default` no schema
    market: Literal["BR", "global"] = Field(default_factory=lambda: "BR")
    topic: str | None = None


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


def build_knowledge_tools(
    index: KnowledgeIndex, search: SearchProvider | None = None
) -> list[ToolSpec]:
    async def retrieve_getnet_knowledge(args: RetrieveArgs, ctx: RequestContext) -> ToolResult:
        query = f"{args.topic}: {args.query}" if args.topic else args.query
        try:
            check_public_query(query, ctx)  # a consulta vira embedding no provedor externo
            ctx.budget.charge_embedding()
        except PrivateDataInQuery as exc:
            return _refuse("retrieve_getnet_knowledge", ctx, exc)
        except BudgetExceeded as exc:
            return ToolResult(status="error", error=str(exc))
        hits = await index.search(query, top_k=TOP_K, market=args.market)
        if not hits:
            return ToolResult(status="empty", data={"chunks": []})
        return ToolResult(
            status="ok",
            data={
                "chunks": [
                    {
                        "chunk_id": h.chunk.chunk_id,
                        "title": h.chunk.title,
                        "url": h.chunk.source_url,
                        "fetched_at": h.chunk.fetched_at.isoformat(),
                        "text": h.chunk.text,
                    }
                    for h in hits
                ]
            },
            evidence=[
                Evidence(
                    id=h.chunk.chunk_id,
                    type="kb_chunk",
                    title=h.chunk.title,
                    url=h.chunk.source_url,
                )
                for h in hits
            ],
        )

    async def web_search(args: WebSearchArgs, ctx: RequestContext) -> ToolResult:
        assert search is not None
        try:
            check_public_query(args.query, ctx)
        except PrivateDataInQuery as exc:
            return _refuse("web_search", ctx, exc)
        # o prazo restante da requisição limita a busca: ela nunca estoura o deadline
        timeout = min(SEARCH_TIMEOUT_SECONDS, ctx.budget.remaining_seconds())
        try:
            results = await asyncio.wait_for(search.search(args.query, max_results=5), timeout)
        except TimeoutError:
            return ToolResult(status="error", error="web search unavailable: timeout")
        except SearchUnavailable as exc:
            return ToolResult(status="error", error=f"web search unavailable: {exc}")
        if not results:
            return ToolResult(status="empty", data={"results": []})
        entries = [
            {"id": f"web_{hashlib.sha1(r.url.encode()).hexdigest()[:10]}", **r.model_dump()}
            for r in results
        ]
        return ToolResult(
            status="ok",
            data={"results": entries},
            evidence=[
                Evidence(id=e["id"], type="web_result", title=e["title"], url=e["url"])
                for e in entries
            ],
        )

    tools = [
        ToolSpec(
            name="retrieve_getnet_knowledge",
            description=(
                "Busca trechos das páginas oficiais da Getnet (produtos, serviços, antecipação, "
                "Pix, crediário, Link de Pagamento, erros de terminal). Use para qualquer "
                "afirmação sobre a Getnet. market: 'BR' (padrão) ou 'global'; topic: assunto curto "
                "ou null."
            ),
            args_model=RetrieveArgs,
            run=retrieve_getnet_knowledge,
        )
    ]
    if search is not None:
        tools.append(
            ToolSpec(
                name="web_search",
                description=(
                    "Busca na web para perguntas gerais ou dependentes de dados atuais (clima, "
                    "cotações, notícias). Envie apenas uma consulta pública, sem dados do cliente."
                ),
                args_model=WebSearchArgs,
                run=web_search,
                uses_web=True,
            )
        )
    return tools
