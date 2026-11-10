"""Busca web (Tavily) atrás de um protocolo, para perguntas gerais e dependentes de dados atuais."""

from typing import Protocol

import httpx
from pydantic import BaseModel

TAVILY_URL = "https://api.tavily.com/search"


class SearchUnavailable(Exception):
    """O provedor de busca falhou ou não respondeu a tempo."""


class WebResult(BaseModel):
    title: str
    url: str
    content: str
    published_date: str | None = None


class SearchProvider(Protocol):
    async def search(self, query: str, max_results: int = 5) -> list[WebResult]: ...


class TavilySearch:
    def __init__(
        self, api_key: str, client: httpx.AsyncClient | None = None, timeout: float = 10.0
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        try:
            response = await self._client.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"query": query, "max_results": max_results, "search_depth": "basic"},
            )
            response.raise_for_status()
            payload = response.json()
            return [
                WebResult(
                    title=item.get("title") or "",
                    url=item["url"],
                    content=item.get("content") or "",
                    published_date=item.get("published_date"),
                )
                for item in payload["results"]
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # inclui payload fora do formato esperado: nunca deve virar 500
            raise SearchUnavailable(type(exc).__name__) from exc
