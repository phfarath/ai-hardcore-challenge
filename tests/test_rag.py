import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.context import Budget, IndexUnavailable, RequestContext
from app.rag import Chunk, KnowledgeIndex, chunk_document, embedding_text, estimate_tokens
from app.tools.knowledge import RetrieveArgs, build_knowledge_tools
from tests.fakes import FakeEmbedder, FrozenClock

NOW = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)
URL_A = "https://site.getnet.com.br/get-smart/"
URL_B = "https://site.getnet.com.br/duvidas/"


def make_chunk(url: str, text: str, n: int = 1, title: str = "Página") -> Chunk:
    return chunk_document(text, source_url=url, title=title, fetched_at=NOW)[n - 1]


def ctx() -> RequestContext:
    return RequestContext(
        request_id="r", user_id="cliente1988", clock=FrozenClock(), budget=Budget()
    )


def paragraphs(n: int, chars: int = 400) -> str:
    return "\n\n".join(f"P{i} " + ("palavra " * (chars // 8)) for i in range(n))


def test_chunk_respects_max_tokens_and_overlap() -> None:
    chunks = chunk_document(
        paragraphs(10),
        source_url=URL_A,
        title="T",
        fetched_at=NOW,
        max_tokens=250,
        overlap_tokens=120,
    )
    assert len(chunks) > 1
    assert all(estimate_tokens(c.text) <= 250 for c in chunks)
    first_tail = chunks[0].text.split("\n\n")[-1]
    assert first_tail in chunks[1].text


def test_faq_pair_stays_in_one_chunk() -> None:
    answer = "Sim, você precisa de uma conta. " * 5
    text = (
        paragraphs(6)
        + f"\n\nPreciso de conta para receber via Pix?\n\n{answer}\n\n"
        + paragraphs(6)
    )
    chunks = chunk_document(text, source_url=URL_B, title="FAQ", fetched_at=NOW, max_tokens=300)
    holder = [c for c in chunks if "Preciso de conta para receber via Pix?" in c.text]
    assert holder
    assert all(answer.strip() in c.text for c in holder)


def test_chunk_metadata_complete_and_hash_stable() -> None:
    a = chunk_document("um texto simples", source_url=URL_A, title="Get Smart", fetched_at=NOW)[0]
    b = chunk_document("um texto simples", source_url=URL_A, title="Get Smart", fetched_at=NOW)[0]
    assert a.content_hash == b.content_hash
    assert a.chunk_id == b.chunk_id
    assert (a.market, a.language, a.source_url, a.title) == ("BR", "pt-BR", URL_A, "Get Smart")
    assert a.fetched_at == NOW


async def test_replace_document_is_idempotent(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    chunks = chunk_document(
        paragraphs(6), source_url=URL_A, title="T", fetched_at=NOW, max_tokens=250
    )
    await index.replace_document(URL_A, chunks)
    await index.replace_document(URL_A, chunks)
    hits = await index.search("qualquer", top_k=50)
    assert len(hits) == len(chunks)


async def test_search_returns_most_similar_with_metadata(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    a = make_chunk(URL_A, "texto sobre a Get Smart", title="Get Smart")
    b = make_chunk(URL_B, "texto sobre o Pix", title="FAQ")
    await index.replace_document(URL_A, [a])
    await index.replace_document(URL_B, [b])
    hits = await index.search(embedding_text(a), top_k=2)
    assert hits[0].chunk == a
    assert hits[0].distance <= hits[1].distance


async def test_search_empty_index_raises_index_unavailable(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    assert index.status() == "missing"
    with pytest.raises(IndexUnavailable):
        await index.search("x")


async def test_search_embedding_model_mismatch_raises(tmp_path: Path) -> None:
    path = str(tmp_path / "idx")
    index = KnowledgeIndex(path, FakeEmbedder())
    await index.replace_document(URL_A, [make_chunk(URL_A, "texto")])
    other = FakeEmbedder()
    other.model = "other-embed"
    reopened = KnowledgeIndex(path, other)
    assert reopened.status() == "incompatible"
    with pytest.raises(IndexUnavailable):
        await reopened.search("x")


async def test_retrieve_tool_evidence_uses_metadata_not_llm_fields(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    chunk = make_chunk(URL_A, "A Get Smart tem tela touch.", title="Get Smart")
    await index.replace_document(URL_A, [chunk])
    tool = {t.name: t for t in build_knowledge_tools(index)}["retrieve_getnet_knowledge"]
    assert "url" not in tool.args_model.model_json_schema()["properties"]
    result = await tool.run(RetrieveArgs(query="Get Smart tela"), ctx())
    assert result.status == "ok"
    assert [(e.id, e.type, e.title, e.url) for e in result.evidence] == [
        (chunk.chunk_id, "kb_chunk", "Get Smart", URL_A)
    ]
    assert "distance" not in result.model_dump_json()


async def test_retrieve_tool_without_hits_for_market_is_empty(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    await index.replace_document(URL_A, [make_chunk(URL_A, "texto")])
    tool = {t.name: t for t in build_knowledge_tools(index)}["retrieve_getnet_knowledge"]
    result = await tool.run(RetrieveArgs(query="x", market="global"), ctx())
    assert result.status == "empty"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"chunks": 3}'])
async def test_corrupt_manifest_is_incompatible_not_a_crash(tmp_path: Path, content: str) -> None:
    path = tmp_path / "idx"
    index = KnowledgeIndex(str(path), FakeEmbedder())
    await index.replace_document(URL_A, [make_chunk(URL_A, "texto")])
    (path / "manifest.json").write_text(content, "utf-8")
    assert index.status() == "incompatible"
    assert index.version is None
    with pytest.raises(IndexUnavailable):
        await index.search("x")


async def test_chroma_query_error_becomes_index_unavailable(tmp_path: Path) -> None:
    from chromadb.errors import ChromaError

    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    await index.replace_document(URL_A, [make_chunk(URL_A, "texto")])

    def boom(*args: object, **kwargs: object) -> None:
        raise ChromaError("dimension mismatch")

    index._collection.query = boom  # type: ignore[method-assign,assignment]
    with pytest.raises(IndexUnavailable):
        await index.search("x")


async def test_prune_removes_dropped_documents_and_updates_manifest(tmp_path: Path) -> None:
    path = tmp_path / "idx"
    index = KnowledgeIndex(str(path), FakeEmbedder())
    await index.replace_document(URL_A, [make_chunk(URL_A, "texto A")])
    await index.replace_document(URL_B, [make_chunk(URL_B, "texto B")])
    await index.prune({URL_A})
    hits = await index.search("x", top_k=10)
    assert {h.chunk.source_url for h in hits} == {URL_A}
    assert json.loads((path / "manifest.json").read_text("utf-8"))["chunks"] == 1


async def test_manifest_write_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "idx"
    index = KnowledgeIndex(str(path), FakeEmbedder())
    await index.replace_document(URL_A, [make_chunk(URL_A, "texto")])
    assert [p.name for p in path.glob("manifest*")] == ["manifest.json"]
