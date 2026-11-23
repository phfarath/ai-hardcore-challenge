"""Dependências de uma execução, injetadas: reais em produção, falsas nos testes."""

from dataclasses import dataclass

from app.auth import Authenticator, DevIssuer
from app.config import Settings
from app.context import Clock
from app.llm import LLMClient
from app.rag import KnowledgeIndex
from app.repositories import CustomerRepository
from app.state import StateStore
from app.tools.search import SearchProvider


@dataclass
class Deps:
    llm: LLMClient
    repo: CustomerRepository
    index: KnowledgeIndex
    search: SearchProvider | None
    clock: Clock
    settings: Settings
    auth: Authenticator
    state: StateStore
    dev_issuer: DevIssuer | None = None

    async def aclose(self) -> None:
        """Fecha os clientes HTTP dos provedores no desligamento (SIGTERM → lifespan)."""
        for resource in (self.llm, self.index, self.search):
            close = getattr(resource, "aclose", None)
            if close is not None:
                await close()
