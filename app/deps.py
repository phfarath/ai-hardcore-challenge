"""Dependências de uma execução, injetadas: reais em produção, falsas nos testes."""

from dataclasses import dataclass

from app.auth import Authenticator, DevIssuer
from app.config import Settings
from app.context import Clock
from app.llm import LLMClient
from app.rag import KnowledgeIndex
from app.repositories import CustomerRepository
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
    dev_issuer: DevIssuer | None = None
