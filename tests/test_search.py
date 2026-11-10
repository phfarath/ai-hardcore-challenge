import json
from pathlib import Path

import httpx
import pytest

from app.context import Budget, RequestContext
from app.rag import KnowledgeIndex
from app.tools.knowledge import WebSearchArgs, build_knowledge_tools
from app.tools.search import SearchUnavailable, TavilySearch, WebResult
from tests.fakes import FakeEmbedder, FakeSearch, FrozenClock

RESULT = WebResult(
    title="Euro hoje",
    url="https://example.test/euro",
    content="O euro fechou a R$ 6,10.",
    published_date="2026-11-07",
)


def ctx() -> RequestContext:
    return RequestContext(
        request_id="r", user_id="cliente1988", clock=FrozenClock(), budget=Budget()
    )


def web_tool(search: FakeSearch, tmp_path: Path):  # type: ignore[no-untyped-def]
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    return {t.name: t for t in build_knowledge_tools(index, search)}["web_search"]


async def test_tavily_parses_results_with_httpx_mock() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Euro hoje",
                        "url": "https://example.test/euro",
                        "content": "O euro fechou a R$ 6,10.",
                        "score": 0.9,
                        "published_date": "2026-11-07",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results = await TavilySearch("tvly-test", client=client).search(
        "cotação do euro", max_results=3
    )
    assert results == [RESULT]
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["auth"] == "Bearer tvly-test"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["query"] == "cotação do euro"
    assert body["max_results"] == 3


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500, json={"detail": "boom"}), httpx.ReadTimeout("slow")],
)
async def test_tavily_http_error_raises_search_unavailable(
    response: httpx.Response | httpx.ReadTimeout,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SearchUnavailable):
        await TavilySearch("k", client=client).search("x")


async def test_web_search_tool_unavailable_returns_error_result(tmp_path: Path) -> None:
    tool = web_tool(FakeSearch(raises=SearchUnavailable("down")), tmp_path)
    result = await tool.run(WebSearchArgs(query="euro"), ctx())
    assert result.status == "error"
    assert result.evidence == []


async def test_web_search_tool_no_results_is_empty(tmp_path: Path) -> None:
    result = await web_tool(FakeSearch(results=[]), tmp_path).run(WebSearchArgs(query="x"), ctx())
    assert result.status == "empty"


async def test_web_search_tool_evidence_ids_are_stable(tmp_path: Path) -> None:
    tool = web_tool(FakeSearch(results=[RESULT]), tmp_path)
    first = await tool.run(WebSearchArgs(query="euro"), ctx())
    second = await tool.run(WebSearchArgs(query="euro"), ctx())
    assert first.status == "ok"
    assert first.evidence[0].id == second.evidence[0].id
    assert first.evidence[0].id.startswith("web_")
    assert (first.evidence[0].type, first.evidence[0].url) == ("web_result", RESULT.url)
    assert tool.uses_web is True


async def test_web_tool_absent_without_search_provider(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    names = {t.name for t in build_knowledge_tools(index)}
    assert names == {"retrieve_getnet_knowledge"}


@pytest.mark.parametrize(
    "payload",
    [[], {"results": "oops"}, {"results": [{"title": "sem url"}]}, {"results": [None]}],
)
async def test_tavily_malformed_payload_raises_search_unavailable(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SearchUnavailable):
        await TavilySearch("k", client=client).search("x")
