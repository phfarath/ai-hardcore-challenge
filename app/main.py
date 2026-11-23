"""API HTTP: POST /chat (autenticado), GET /health e, só em desenvolvimento, POST /dev/token."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
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
from app.deps import Deps
from app.llm import OpenAIClient
from app.observability import configure_logging, log_event
from app.orchestrator import handle_chat
from app.policies import (
    CustomerSelectionRequired,
    Forbidden,
    InsufficientScope,
    ResourceNotFound,
    authorize_chat,
)
from app.rag import KnowledgeIndex, OpenAIEmbedder
from app.repositories import JsonCustomerRepository
from app.schemas import ChatRequest, ChatResponse
from app.tools.search import TavilySearch


class Utf8JSONResponse(JSONResponse):
    """Declara o charset: clientes como o PowerShell 5.1 assumem Latin-1 quando ele falta e
    exibem acentos quebrados."""

    media_type = "application/json; charset=utf-8"


FIXTURES = DATA_DIR / "fixtures" / "customers.json"


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
            reasoning_effort=settings.openai_reasoning_effort,
        ),
        repo=JsonCustomerRepository(FIXTURES),
        index=KnowledgeIndex(settings.chroma_path, embedder),
        search=TavilySearch(tavily_key) if tavily_key else None,
        clock=SystemClock(settings.timezone),
        settings=settings,
        auth=auth,
        dev_issuer=dev_issuer,
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
        yield

    app = FastAPI(
        title="Getnet Multi-Agent Support",
        lifespan=lifespan,
        default_response_class=Utf8JSONResponse,
    )
    app.state.deps = deps

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
        req: ChatRequest, principal: Annotated[Principal, Depends(current_principal)]
    ) -> ChatResponse:
        # autorização antes do Router: recusa aqui não gasta nenhuma chamada ao LLM
        authorized = authorize_chat(principal, req.message, req.user_id)
        return await handle_chat(authorized, app.state.deps)

    @app.post("/dev/token")
    async def dev_token(req: DevTokenRequest) -> dict[str, str]:
        """Só em APP_ENV=development: emite token do emissor local para um principal de
        desenvolvimento. Em produção a rota responde 404 e o emissor nem existe."""
        issuer = app.state.deps.dev_issuer
        if issuer is None or req.sub not in issuer.profiles:
            raise ResourceNotFound("dev issuer disabled or unknown principal")
        return {"access_token": issuer.issue(req.sub, scopes=req.scopes), "token_type": "Bearer"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "index": app.state.deps.index.status()}

    return app


app = create_app()
