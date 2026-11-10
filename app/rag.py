"""RAG: chunking, embeddings e índice vetorial persistente (Chroma)."""

import asyncio
import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import chromadb
import openai
from chromadb import Where
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import ChromaError
from pydantic import BaseModel

from app.context import IndexUnavailable, ProviderUnavailable


class Chunk(BaseModel):
    chunk_id: str
    text: str
    source_url: str
    title: str
    market: str
    language: str
    fetched_at: datetime
    content_hash: str


class RetrievedChunk(BaseModel):
    chunk: Chunk
    distance: float


def estimate_tokens(text: str) -> int:
    """Estimativa barata (~4 caracteres por token); evita baixar vocabulário de tokenizer."""
    return math.ceil(len(text) / 4)


def embedding_text(chunk: Chunk) -> str:
    """Texto efetivamente embutido: o título ajuda páginas cujo corpo não repete o produto."""
    return f"{chunk.title}\n\n{chunk.text}"


def _units(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    i = 0
    while i < len(paragraphs):
        # pergunta + resposta de FAQ ficam juntas
        if paragraphs[i].endswith("?") and i + 1 < len(paragraphs):
            units.append(paragraphs[i] + "\n\n" + paragraphs[i + 1])
            i += 2
        else:
            units.append(paragraphs[i])
            i += 1
    return units


def _split_oversized(unit: str, max_tokens: int) -> list[str]:
    limit = max_tokens * 4
    if len(unit) <= limit:
        return [unit]
    parts: list[str] = []
    current = ""
    for word in unit.split():
        if current and len(current) + 1 + len(word) > limit:
            parts.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        parts.append(current)
    return parts


def _slug(url: str) -> str:
    parsed = urlparse(url)
    return re.sub(r"[^a-z0-9]+", "-", f"{parsed.netloc}{parsed.path}".lower()).strip("-")


def chunk_document(
    text: str,
    *,
    source_url: str,
    title: str,
    fetched_at: datetime,
    market: str = "BR",
    language: str = "pt-BR",
    max_tokens: int = 700,
    overlap_tokens: int = 80,
) -> list[Chunk]:
    units = [part for u in _units(text) for part in _split_oversized(u, max_tokens)]
    texts: list[str] = []
    current: list[str] = []
    for unit in units:
        if current and estimate_tokens("\n\n".join([*current, unit])) > max_tokens:
            texts.append("\n\n".join(current))
            tail = current[-1]
            keep = (
                estimate_tokens(tail) <= overlap_tokens
                and estimate_tokens(f"{tail}\n\n{unit}") <= max_tokens
            )
            current = [tail] if keep else []
        current.append(unit)
    if current:
        texts.append("\n\n".join(current))

    slug = _slug(source_url)
    return [
        Chunk(
            chunk_id=f"{slug}-{n:03d}",
            text=t,
            source_url=source_url,
            title=title,
            market=market,
            language=language,
            fetched_at=fetched_at,
            content_hash=hashlib.sha256(t.encode("utf-8")).hexdigest()[:16],
        )
        for n, t in enumerate(texts, start=1)
    ]


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str, timeout: float = 30.0) -> None:
        self.model = model
        self._client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=1)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._client.embeddings.create(model=self.model, input=texts)
        except openai.OpenAIError as exc:
            raise ProviderUnavailable(type(exc).__name__) from exc
        return [item.embedding for item in response.data]


IndexStatus = Literal["ready", "missing", "incompatible"]


class KnowledgeIndex:
    """Índice vetorial persistente. Um manifesto JSON registra modelo/dimensão dos embeddings:
    consultar com outro modelo seria comparar vetores incompatíveis, então é recusado."""

    _COLLECTION = "getnet_knowledge"

    def __init__(self, path: str, embedder: Embedder) -> None:
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        client = chromadb.PersistentClient(
            path=str(self._path), settings=ChromaSettings(anonymized_telemetry=False)
        )
        self._collection = client.get_or_create_collection(
            self._COLLECTION, metadata={"hnsw:space": "cosine"}
        )

    @property
    def _manifest_file(self) -> Path:
        return self._path / "manifest.json"

    def _read_manifest(self) -> dict[str, Any] | None:
        """None se não existe; `IndexUnavailable` se existe mas está corrompido."""
        if not self._manifest_file.exists():
            return None
        try:
            data = json.loads(self._manifest_file.read_text("utf-8"))
            if not {"embedding_model", "embedding_dim", "chunks", "updated_at"} <= set(data):
                raise KeyError("manifest keys")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise IndexUnavailable("index manifest is corrupted") from exc
        manifest: dict[str, Any] = data
        return manifest

    @property
    def version(self) -> str | None:
        try:
            manifest = self._read_manifest()
        except IndexUnavailable:
            return None
        return None if manifest is None else str(manifest["updated_at"])

    def status(self) -> IndexStatus:
        try:
            manifest = self._read_manifest()
        except IndexUnavailable:
            return "incompatible"
        if manifest is None or manifest["chunks"] == 0:
            return "missing"
        if manifest["embedding_model"] != self._embedder.model:
            return "incompatible"
        return "ready"

    async def replace_document(self, source_url: str, chunks: list[Chunk]) -> None:
        vectors = await self._embedder.embed([embedding_text(c) for c in chunks])
        manifest = self._read_manifest()
        if manifest is not None and (
            manifest["embedding_model"] != self._embedder.model
            or manifest["embedding_dim"] != len(vectors[0])
        ):
            raise IndexUnavailable("index was built with a different embedding model")
        await asyncio.to_thread(self._write, source_url, chunks, vectors)

    def _write(self, source_url: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        self._collection.delete(where={"source_url": source_url})
        self._collection.add(
            ids=[c.chunk_id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=vectors,  # type: ignore[arg-type]
            metadatas=[
                {
                    "chunk_id": c.chunk_id,
                    "source_url": c.source_url,
                    "title": c.title,
                    "market": c.market,
                    "language": c.language,
                    "fetched_at": c.fetched_at.isoformat(),
                    "content_hash": c.content_hash,
                }
                for c in chunks
            ],
        )
        self._write_manifest(len(vectors[0]))

    def _write_manifest(self, dim: int) -> None:
        """Escrita atômica (temporário + rename): uma queda no meio não corrompe o índice."""
        temp = self._manifest_file.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(
                {
                    "embedding_model": self._embedder.model,
                    "embedding_dim": dim,
                    "chunks": self._collection.count(),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ),
            "utf-8",
        )
        os.replace(temp, self._manifest_file)

    async def prune(self, keep_urls: set[str]) -> None:
        """Remove do índice os documentos cuja URL não está mais em `keep_urls`."""
        await asyncio.to_thread(self._prune, keep_urls)

    def _prune(self, keep_urls: set[str]) -> None:
        manifest = self._read_manifest()
        if manifest is None or not keep_urls:
            return
        keep: list[str | int | float | bool] = [*sorted(keep_urls)]
        # o Chroma tipa os operadores como Literal e o mypy infere str
        where: Where = {"source_url": {"$nin": keep}}  # type: ignore[dict-item]
        self._collection.delete(where=where)
        self._write_manifest(int(manifest["embedding_dim"]))

    async def search(
        self, query: str, *, top_k: int = 5, market: str | None = None
    ) -> list[RetrievedChunk]:
        if self.status() != "ready":
            raise IndexUnavailable(f"knowledge index is {self.status()}")
        [vector] = await self._embedder.embed([query])
        return await asyncio.to_thread(self._query, vector, top_k, market)

    def _query(self, vector: list[float], top_k: int, market: str | None) -> list[RetrievedChunk]:
        try:
            count = self._collection.count()
            result = self._collection.query(
                query_embeddings=[vector],  # type: ignore[arg-type]
                n_results=max(1, min(top_k, count)),
                where={"market": market} if market else None,
            )
        except ChromaError as exc:
            raise IndexUnavailable(f"vector store error: {type(exc).__name__}") from exc
        documents = (result["documents"] or [[]])[0]
        metadatas = (result["metadatas"] or [[]])[0]
        distances = (result["distances"] or [[]])[0]
        hits: list[RetrievedChunk] = []
        for text, meta, distance in zip(documents, metadatas, distances, strict=True):
            hits.append(
                RetrievedChunk(
                    chunk=Chunk(
                        chunk_id=str(meta["chunk_id"]),
                        text=text,
                        source_url=str(meta["source_url"]),
                        title=str(meta["title"]),
                        market=str(meta["market"]),
                        language=str(meta["language"]),
                        fetched_at=datetime.fromisoformat(str(meta["fetched_at"])),
                        content_hash=str(meta["content_hash"]),
                    ),
                    distance=float(distance),
                )
            )
        return hits
