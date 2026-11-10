"""Verificações com serviços reais. Exigem `.env` (OPENAI_API_KEY, TAVILY_API_KEY) e o índice
ingerido (`python -m app.ingest`). Rodar com: python -m pytest -m live"""

import pytest

from app.config import Settings
from app.rag import KnowledgeIndex, OpenAIEmbedder
from app.tools.search import TavilySearch

pytestmark = pytest.mark.live

BASE = "https://site.getnet.com.br"
RAG_QUESTIONS = [
    (
        "diferença entre a Get Clássica e a Get Smart",
        [f"{BASE}/maquininha/get-classica/", f"{BASE}/maquininha/aluguel/"],
    ),
    (
        "preciso de conta bancária para receber vendas por Pix?",
        [
            f"{BASE}/pix/",
            f"{BASE}/pix-na-maquininha-getnet/",
            f"{BASE}/get-ajuda-pix/como-habilitar-o-recebimento-de-vendas-via-pix-no-aplicativo-getnet-brasil/",
            f"{BASE}/conta-digital/",
            f"{BASE}/get-ajuda-get-conta/o-que-e-a-get-conta/",
        ],
    ),
    (
        "como funciona a antecipação de vendas",
        [
            f"{BASE}/get-ajuda-antecipacao-de-venda/como-antecipar-sua-vendas-pelo-app/",
            f"{BASE}/duvidas/",
        ],
    ),
    ("em quantas parcelas posso vender no crediário", [f"{BASE}/crediario/", f"{BASE}/duvidas/"]),
    (
        "posso vender pelo WhatsApp com Link de Pagamento",
        [
            f"{BASE}/vender-pelo-whatsapp/",
            f"{BASE}/link-de-pagamento/",
            f"{BASE}/link-de-pagamento-vendas/",
        ],
    ),
    (
        "erro ADQ 1-19 de conexão da maquininha",
        [f"{BASE}/get-ajuda-erros-maquininha/erro-adq-1-19/"],
    ),
]


def _index() -> KnowledgeIndex:
    settings = Settings()
    key = settings.openai_api_key.get_secret_value()
    if not key:
        pytest.skip("OPENAI_API_KEY not set")
    index = KnowledgeIndex(
        settings.chroma_path, OpenAIEmbedder(key, settings.openai_embedding_model)
    )
    if index.status() != "ready":
        pytest.skip("index not ingested: run `python -m app.ingest` first")
    return index


@pytest.mark.parametrize(("question", "expected_urls"), RAG_QUESTIONS)
async def test_live_retrieval_returns_expected_page_in_top5(
    question: str, expected_urls: list[str]
) -> None:
    hits = await _index().search(question, top_k=5)
    assert any(h.chunk.source_url in expected_urls for h in hits), [
        h.chunk.source_url for h in hits
    ]


async def test_live_tavily_returns_results_for_euro_rate() -> None:
    key = Settings().tavily_api_key.get_secret_value()
    if not key:
        pytest.skip("TAVILY_API_KEY not set")
    results = await TavilySearch(key).search("cotação do euro hoje")
    assert results
    assert all(r.url.startswith("http") for r in results)
