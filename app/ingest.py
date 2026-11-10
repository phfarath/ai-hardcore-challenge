"""Ingestão: fontes oficiais → texto principal → chunks → embeddings → índice.

Uso: `python -m app.ingest` (comando separado da API; repetível sem duplicar chunks)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import trafilatura
from pydantic import BaseModel

from app.config import DATA_DIR, Settings
from app.context import IndexUnavailable, ProviderUnavailable
from app.rag import KnowledgeIndex, OpenAIEmbedder, chunk_document

MIN_CHARS = 200
SOURCES_FILE = DATA_DIR / "sources.json"

Fetcher = Callable[[str], Awaitable[str]]


class Source(BaseModel):
    url: str
    title_hint: str | None = None
    market: str = "BR"
    language: str = "pt-BR"


class IngestReport(BaseModel):
    pages_ok: int
    pages_failed: list[tuple[str, str]]
    chunks: int


def extract_main_text(html: str, url: str) -> tuple[str, str]:
    """Devolve (título, texto principal) sem menus, rodapés e formulários repetidos."""
    text = trafilatura.extract(
        html, url=url, include_comments=False, include_tables=True, favor_recall=True
    )
    metadata = trafilatura.extract_metadata(html)
    title = (metadata.title if metadata and metadata.title else "") or ""
    return title, text or ""


async def ingest(
    sources: list[Source], index: KnowledgeIndex, fetch: Fetcher, now: datetime
) -> IngestReport:
    ok = 0
    chunk_total = 0
    failed: list[tuple[str, str]] = []
    for source in sources:
        try:
            html = await fetch(source.url)
        except httpx.HTTPError as exc:
            failed.append((source.url, f"fetch failed: {type(exc).__name__}"))
            continue
        title, text = extract_main_text(html, source.url)
        if len(text) < MIN_CHARS:
            failed.append((source.url, "no useful content extracted"))
            continue
        chunks = chunk_document(
            text,
            source_url=source.url,
            title=title or source.title_hint or source.url,
            fetched_at=now,
            market=source.market,
            language=source.language,
        )
        try:
            await index.replace_document(source.url, chunks)
        except (ProviderUnavailable, IndexUnavailable) as exc:
            failed.append((source.url, f"embedding failed: {exc}"))
            continue
        ok += 1
        chunk_total += len(chunks)
    # páginas retiradas do manifesto saem do índice; as que só falharam agora ficam (dado velho
    # é melhor que nenhum)
    await index.prune({s.url for s in sources})
    return IngestReport(pages_ok=ok, pages_failed=failed, chunks=chunk_total)


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    return [Source(**s) for s in json.loads(path.read_text("utf-8"))]


def exit_code(report: IngestReport) -> int:
    return 0 if report.pages_ok > 0 else 1


async def main(settings: Settings | None = None) -> int:
    settings = settings or Settings()
    if not settings.openai_api_key.get_secret_value():
        print("OPENAI_API_KEY is not set (see .env.example)")
        return 2
    sources = load_sources()
    embedder = OpenAIEmbedder(
        settings.openai_api_key.get_secret_value(), settings.openai_embedding_model
    )
    index = KnowledgeIndex(settings.chroma_path, embedder)
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, headers={"User-Agent": "getnet-support-ingest/1.0"}
    ) as client:

        async def fetch(url: str) -> str:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

        report = await ingest(sources, index, fetch, datetime.now(UTC))
    print(f"pages ok: {report.pages_ok}  chunks: {report.chunks}")
    for url, reason in report.pages_failed:
        print(f"FAILED {url}: {reason}")
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
