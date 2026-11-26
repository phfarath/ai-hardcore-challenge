"""API HTTP: POST /chat (autenticado), GET /health e, só em desenvolvimento, POST /dev/token."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticationError, Principal, build_auth
from app.config import DATA_DIR, Settings
from app.context import (
    BudgetExceeded,
    DeadlineExceeded,
    IndexUnavailable,
    ProviderUnavailable,
    SystemClock,
    VerificationUnavailable,
)
from app.conversations import (
    IdempotencyConflict,
    RequestInProgress,
    authorize_conversation,
    conversation_view,
    handle_turn,
)
from app.deps import Deps
from app.handoffs import (
    ClaimRequest,
    OperatorDecision,
    ResumeWorker,
    customer_handoff,
    customer_view,
    operator_handoff,
    operator_view,
)
from app.llm import OpenAIClient
from app.observability import configure_logging, log_event
from app.policies import (
    HANDOFFS_READ,
    HANDOFFS_RESOLVE,
    CustomerSelectionRequired,
    Forbidden,
    InsufficientScope,
    ResourceNotFound,
    authorize_chat,
    authorize_operator,
)
from app.rag import KnowledgeIndex, OpenAIEmbedder
from app.repositories import JsonCustomerRepository
from app.schemas import ChatRequest, ChatResponse
from app.state import (
    ConversationBusy,
    ConversationConflict,
    HandoffConflict,
    HandoffExpired,
    HandoffState,
    SqliteStateStore,
)
from app.tools.search import TavilySearch


class Utf8JSONResponse(JSONResponse):
    """Declara o charset: clientes como o PowerShell 5.1 assumem Latin-1 quando ele falta e
    exibem acentos quebrados."""

    media_type = "application/json; charset=utf-8"


FIXTURES = DATA_DIR / "fixtures" / "customers.json"


class Overloaded(Exception):
    """O limite de /chat simultâneos do processo foi atingido."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"limit {limit}")
        self.limit = limit


def build_deps(settings: Settings | None = None) -> Deps:
    """Monta as dependências reais. Falha cedo, com mensagem clara, se faltar configuração."""
    settings = settings or Settings()
    openai_key = settings.openai_api_key.get_secret_value()
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY is not set (see .env.example)")
    if not settings.openai_chat_model:
        raise RuntimeError("OPENAI_CHAT_MODEL is not set (see .env.example)")
    tavily_key = settings.tavily_api_key.get_secret_value()
    embedder = OpenAIEmbedder(openai_key, settings.openai_embedding_model)
    auth, dev_issuer = build_auth(settings)
    return Deps(
        llm=OpenAIClient(
            openai_key,
            settings.openai_chat_model,
            timeout=settings.llm_timeout_seconds,
            reasoning_effort=settings.openai_reasoning_effort,
        ),
        repo=JsonCustomerRepository(FIXTURES),
        index=KnowledgeIndex(settings.chroma_path, embedder),
        search=TavilySearch(tavily_key) if tavily_key else None,
        clock=SystemClock(settings.timezone),
        settings=settings,
        auth=auth,
        dev_issuer=dev_issuer,
        state=SqliteStateStore(settings.state_path),
    )


class DevTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub: str
    scopes: list[str] | None = None


def create_app(deps: Deps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        if app.state.deps is None:
            app.state.deps = build_deps()
        deps = app.state.deps
        stop = asyncio.Event()
        task = None
        if deps.settings.resume_worker_enabled:
            worker = ResumeWorker(deps.state, deps.auth.access, deps.clock)
            task = asyncio.create_task(worker.run(stop))
        app.state.draining = False
        yield
        # desligamento: sai do balanceamento, para o worker e fecha os clientes dos provedores
        app.state.draining = True
        stop.set()
        if task is not None:
            await task
        await deps.aclose()
        log_event("shutdown", status="clients_closed")

    app = FastAPI(
        title="Getnet Multi-Agent Support",
        lifespan=lifespan,
        default_response_class=Utf8JSONResponse,
    )
    app.state.deps = deps
    app.state.draining = False
    in_flight = {"chats": 0}

    def error(status_code: int, code: str) -> JSONResponse:
        return Utf8JSONResponse(status_code=status_code, content={"error": code})

    @app.exception_handler(RequestValidationError)
    async def _invalid_payload(_: Request, exc: RequestValidationError) -> JSONResponse:
        # mesmo corpo do handler padrão do FastAPI, mas com charset declarado
        return Utf8JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    @app.exception_handler(AuthenticationError)
    async def _unauthenticated(_: Request, exc: AuthenticationError) -> JSONResponse:
        log_event("auth_failed", error_code="AuthenticationError", detail=str(exc))
        response = error(401, "unauthorized")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(InsufficientScope)
    async def _scope(_: Request, exc: InsufficientScope) -> JSONResponse:
        log_event("access_denied", error_code="InsufficientScope", detail=str(exc))
        return error(403, "insufficient_scope")

    @app.exception_handler(Forbidden)
    async def _forbidden(_: Request, exc: Forbidden) -> JSONResponse:
        log_event("access_denied", error_code="Forbidden", detail=str(exc))
        return error(403, "forbidden")

    @app.exception_handler(ResourceNotFound)
    async def _not_found(_: Request, __: ResourceNotFound) -> JSONResponse:
        return error(404, "not_found")

    @app.exception_handler(CustomerSelectionRequired)
    async def _selection(_: Request, __: CustomerSelectionRequired) -> JSONResponse:
        return error(400, "customer_selection_required")

    @app.exception_handler(Overloaded)
    async def _overloaded(_: Request, exc: Overloaded) -> JSONResponse:
        log_event("overloaded", limit=exc.limit)
        response = error(503, "overloaded")
        response.headers["Retry-After"] = "2"
        return response

    @app.exception_handler(HandoffExpired)
    async def _handoff_expired(_: Request, __: HandoffExpired) -> JSONResponse:
        return error(409, "handoff_expired")

    @app.exception_handler(HandoffConflict)
    async def _handoff_conflict(_: Request, exc: HandoffConflict) -> JSONResponse:
        log_event("handoff_conflict", detail=str(exc))
        return error(409, "handoff_conflict")

    @app.exception_handler(IdempotencyConflict)
    async def _idempotency(_: Request, __: IdempotencyConflict) -> JSONResponse:
        return error(409, "idempotency_key_reused")

    @app.exception_handler(RequestInProgress)
    async def _in_progress(_: Request, __: RequestInProgress) -> JSONResponse:
        return error(409, "request_in_progress")

    @app.exception_handler(ConversationBusy)
    async def _busy(_: Request, __: ConversationBusy) -> JSONResponse:
        return error(409, "conversation_busy")

    @app.exception_handler(ConversationConflict)
    async def _conflict(_: Request, __: ConversationConflict) -> JSONResponse:
        return error(409, "conversation_conflict")

    @app.exception_handler(ProviderUnavailable)
    async def _llm_down(_: Request, exc: ProviderUnavailable) -> JSONResponse:
        log_event("request_failed", error_code="ProviderUnavailable", detail=str(exc))
        return error(503, "llm_unavailable")

    @app.exception_handler(IndexUnavailable)
    async def _index_down(_: Request, exc: IndexUnavailable) -> JSONResponse:
        log_event("request_failed", error_code="IndexUnavailable", detail=str(exc))
        return error(503, "index_unavailable")

    @app.exception_handler(VerificationUnavailable)
    async def _verification_down(_: Request, exc: VerificationUnavailable) -> JSONResponse:
        log_event("request_failed", error_code="VerificationUnavailable", detail=str(exc))
        return error(503, "verification_unavailable")

    @app.exception_handler(DeadlineExceeded)
    async def _deadline(_: Request, __: DeadlineExceeded) -> JSONResponse:
        return error(504, "deadline_exceeded")

    @app.exception_handler(BudgetExceeded)
    async def _budget(_: Request, __: BudgetExceeded) -> JSONResponse:
        return error(503, "budget_exceeded")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log_event("request_failed", error_code=type(exc).__name__)
        return error(500, "internal_error")

    bearer = HTTPBearer(auto_error=False)

    async def current_principal(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if credentials is None:
            raise AuthenticationError("missing bearer token")
        principal: Principal = await app.state.deps.auth.authenticate(credentials.credentials)
        return principal

    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        req: ChatRequest,
        principal: Annotated[Principal, Depends(current_principal)],
        idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
    ) -> ChatResponse:
        # autorização antes do Router: recusa aqui não gasta nenhuma chamada ao LLM
        authorized = authorize_chat(principal, req.message, req.user_id)
        limit = app.state.deps.settings.max_concurrent_chats
        if in_flight["chats"] >= limit:
            # saturação: recusa imediata e previsível, em vez de fila sem prazo
            raise Overloaded(limit)
        in_flight["chats"] += 1
        try:
            return await handle_turn(
                authorized,
                app.state.deps,
                conversation_id=req.conversation_id,
                idempotency_key=idempotency_key,
            )
        finally:
            in_flight["chats"] -= 1

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        principal: Annotated[Principal, Depends(current_principal)],
        user_id: str | None = None,
    ) -> dict[str, object]:
        deps = app.state.deps
        authorized = authorize_chat(principal, "-", user_id)
        now = deps.clock.now().timestamp()
        conversation = authorize_conversation(
            deps.state.get_conversation(conversation_id), authorized, now
        )
        return conversation_view(deps.state.messages(conversation.id), conversation)

    @app.post("/dev/token")
    async def dev_token(req: DevTokenRequest) -> dict[str, str]:
        """Só em APP_ENV=development: emite token do emissor local para um principal de
        desenvolvimento. Em produção a rota responde 404 e o emissor nem existe."""
        issuer = app.state.deps.dev_issuer
        if issuer is None or req.sub not in issuer.profiles:
            raise ResourceNotFound("dev issuer disabled or unknown principal")
        return {"access_token": issuer.issue(req.sub, scopes=req.scopes), "token_type": "Bearer"}

    @app.get("/handoffs/{handoff_id}")
    async def get_handoff(
        handoff_id: str, principal: Annotated[Principal, Depends(current_principal)]
    ) -> dict[str, object]:
        deps = app.state.deps
        handoff = customer_handoff(deps.state, principal, handoff_id, HANDOFFS_READ)
        return customer_view(handoff, deps.clock.now().timestamp())

    @app.get("/operator/handoffs")
    async def operator_queue(
        principal: Annotated[Principal, Depends(current_principal)],
        state: HandoffState | None = "pending",
    ) -> list[dict[str, object]]:
        authorize_operator(principal, principal.tenant_id, HANDOFFS_READ)
        deps = app.state.deps
        now = deps.clock.now().timestamp()
        handoffs = deps.state.list_handoffs(principal.tenant_id, state)
        views = [operator_view(h, now) for h in handoffs]
        return [v for v in views if state is None or v["state"] == state]

    @app.get("/operator/handoffs/{handoff_id}")
    async def operator_detail(
        handoff_id: str, principal: Annotated[Principal, Depends(current_principal)]
    ) -> dict[str, object]:
        deps = app.state.deps
        handoff = operator_handoff(deps.state, principal, handoff_id, HANDOFFS_READ)
        return operator_view(handoff, deps.clock.now().timestamp())

    @app.post("/operator/handoffs/{handoff_id}/claim")
    async def operator_claim(
        handoff_id: str,
        req: ClaimRequest,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> dict[str, object]:
        deps = app.state.deps
        operator_handoff(deps.state, principal, handoff_id, HANDOFFS_RESOLVE)
        now = deps.clock.now().timestamp()
        claimed = deps.state.claim_handoff(handoff_id, principal.sub, req.expected_version, now)
        return operator_view(claimed, now)

    @app.post("/operator/handoffs/{handoff_id}/resolve")
    async def operator_resolve(
        handoff_id: str,
        req: OperatorDecision,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> dict[str, object]:
        deps = app.state.deps
        operator_handoff(deps.state, principal, handoff_id, HANDOFFS_RESOLVE)
        decision = req.model_dump(exclude={"expected_version"}) | {"operator": principal.sub}
        now = deps.clock.now().timestamp()
        resolved = deps.state.resolve_handoff(
            handoff_id, principal.sub, req.expected_version, decision, now
        )
        return operator_view(resolved, now)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness: o processo responde. Não depende de provedores externos."""
        return {"status": "ok", "index": app.state.deps.index.status()}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness: pode receber tráfego? Índice pronto, estado acessível e fora do
        desligamento. Não chama o LLM: prontidão não pode gastar cota."""
        deps = app.state.deps
        checks = {
            "index": deps.index.status(),
            "state": "ok" if await asyncio.to_thread(deps.state.ping) else "unavailable",
            "lifecycle": "draining" if app.state.draining else "running",
        }
        ok = checks == {"index": "ready", "state": "ok", "lifecycle": "running"}
        body = {"status": "ready" if ok else "not_ready", "checks": checks}
        return Utf8JSONResponse(status_code=200 if ok else 503, content=body)

    return app


app = create_app()
