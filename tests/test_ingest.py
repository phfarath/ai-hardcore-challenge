from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.ingest import IngestReport, Source, exit_code, extract_main_text, ingest
from app.rag import KnowledgeIndex
from tests.fakes import FakeEmbedder

NOW = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)
GOOD_HTML = Path("tests/fixtures/page_with_menu.html").read_text("utf-8")
GOOD = "https://site.getnet.com.br/get-smart/"
BROKEN = "https://site.getnet.com.br/quebrada/"
EMPTY = "https://site.getnet.com.br/vazia/"


async def fake_fetch(url: str) -> str:
    if url == BROKEN:
        raise httpx.ConnectError("boom")
    if url == EMPTY:
        return "<html><body><p>oi</p></body></html>"
    return GOOD_HTML


def sources() -> list[Source]:
    return [Source(url=u) for u in (BROKEN, EMPTY, GOOD)]


def test_extract_main_text_drops_nav_and_footer() -> None:
    title, text = extract_main_text(GOOD_HTML, GOOD)
    assert "Get Smart" in title
    assert "tela touch" in text
    assert "A Get Smart funciona sem internet?" in text
    assert "Menu principal" not in text
    assert "Rodapé institucional" not in text


async def test_ingest_reports_failures_and_indexes_the_rest(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    report = await ingest(sources(), index, fake_fetch, NOW)
    assert report.pages_ok == 1
    assert {url for url, _ in report.pages_failed} == {BROKEN, EMPTY}
    assert all(reason for _, reason in report.pages_failed)
    assert report.chunks >= 1
    assert index.status() == "ready"


async def test_ingest_twice_does_not_duplicate_chunks(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    first = await ingest(sources(), index, fake_fetch, NOW)
    second = await ingest(sources(), index, fake_fetch, NOW)
    hits = await index.search("Get Smart", top_k=100)
    assert first.chunks == second.chunks == len(hits)


def test_cli_exit_code_nonzero_only_when_all_fail() -> None:
    assert exit_code(IngestReport(pages_ok=1, pages_failed=[("u", "r")], chunks=3)) == 0
    assert exit_code(IngestReport(pages_ok=0, pages_failed=[("u", "r")], chunks=0)) == 1


async def test_main_fails_fast_without_openai_key() -> None:
    from pydantic import SecretStr

    from app.config import Settings
    from app.ingest import main

    settings = Settings(_env_file=None, openai_api_key=SecretStr(""))  # type: ignore[call-arg]
    assert await main(settings) == 2


async def test_reingest_prunes_pages_dropped_from_sources(tmp_path: Path) -> None:
    other = "https://site.getnet.com.br/outra/"
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    await ingest([Source(url=GOOD), Source(url=other)], index, fake_fetch, NOW)
    await ingest([Source(url=GOOD)], index, fake_fetch, NOW)
    hits = await index.search("Get Smart", top_k=100)
    assert {h.chunk.source_url for h in hits} == {GOOD}


async def test_reingest_keeps_old_chunks_of_a_page_that_failed_this_time(tmp_path: Path) -> None:
    other = "https://site.getnet.com.br/outra/"
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    await ingest([Source(url=GOOD), Source(url=other)], index, fake_fetch, NOW)

    async def other_is_down(url: str) -> str:
        if url == other:
            raise httpx.ConnectError("down")
        return GOOD_HTML

    report = await ingest([Source(url=GOOD), Source(url=other)], index, other_is_down, NOW)
    hits = await index.search("Get Smart", top_k=100)
    assert {h.chunk.source_url for h in hits} == {GOOD, other}
    assert [u for u, _ in report.pages_failed] == [other]
