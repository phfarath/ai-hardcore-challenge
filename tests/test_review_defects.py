"""Defeitos da revisão semântica (docs/evidence/semantic-review.md): D2, D4 e D5."""

import hashlib
from pathlib import Path

import pytest

from app.llm import LLMTurn
from app.orchestrator import handle_chat
from app.schemas import RoutePlan, Task
from app.tools.search import WebResult
from tests.fakes import (
    ADQ_CHUNK_ID,
    ADQ_QUOTE,
    SMART_CHUNK_ID,
    SMART_QUOTE,
    FakeSearch,
    ScriptedLLM,
    authorized,
    claim,
    final_turn,
    kout,
    make_deps,
    missing,
    tool_call_turn,
    verdict,
)

PT_TEMPLATE_FRAGMENTS = (
    "Com base nas fontes consultadas",
    "Não localizei evidência",
    "Pendência",
    "Uma busca anterior",
    "outra formulação",
)


def _plan(question: str, scope: str = "getnet") -> LLMTurn:
    return final_turn(
        RoutePlan(
            workflow="knowledge",
            tasks=[Task(agent="knowledge", question=question, source_scope=scope)],  # type: ignore[arg-type]
        )
    )


@pytest.mark.xfail(strict=True, reason="D2: textos fixos só em português")
async def test_d2_english_question_gets_no_portuguese_template_text(tmp_path: Path) -> None:
    """Pergunta em inglês com lacuna, reparo e lacuna restante: nenhum texto fixo em português,
    nem na resposta nem na dica de reparo enviada ao modelo."""
    question = "What is the Get Smart, how do I fix error ADQ 1-19 and what does it cost?"
    turns = [
        _plan(question),
        tool_call_turn("retrieve_getnet_knowledge", query="Get Smart"),
        final_turn(
            kout(
                "The Get Smart has a touch screen.",
                claim(SMART_CHUNK_ID, SMART_QUOTE, statement="The Get Smart has a touch screen."),
                missing("fix error ADQ 1-19"),
                pending=["check the price with sales"],
            )
        ),
        final_turn(verdict(True, uncovered=["fix error ADQ 1-19", "price"])),
        tool_call_turn("retrieve_getnet_knowledge", query="error ADQ 1-19"),
        final_turn(
            kout(
                "Check the Wi-Fi and restart.",
                claim(ADQ_CHUNK_ID, ADQ_QUOTE, statement="Check the Wi-Fi and restart."),
            )
        ),
        final_turn(verdict(True, uncovered=["price"])),
    ]
    llm = ScriptedLLM(turns)
    deps = await make_deps(tmp_path, llm, max_replans=1)
    response = await handle_chat(authorized(question), deps)
    assert response.plan_changes == ["knowledge_repair"]
    assert response.status == "partial"
    for fragment in PT_TEMPLATE_FRAGMENTS:
        assert fragment not in response.answer, fragment
    assert "Based on the sources consulted" in response.answer
    assert "I could not find enough evidence in the sources consulted for: price." in (
        response.answer
    )
    repair_prompt = llm.calls[4][-1]["content"]
    assert "Uma busca anterior" not in repair_prompt
    assert "different query wording" in repair_prompt


XE_URL = "https://www.xe.com/currencyconverter/convert/?From=EUR&To=BRL"
GOOGLE_URL = "https://www.google.com/finance/quote/EUR-BRL"
STALE = "1 EUR = 6,05 BRL em 22/11/2026, 09:00 UTC"
TODAY = "1 EUR = 6,12 BRL em 25/11/2026, 14:00 UTC"


def _web_id(url: str) -> str:
    return f"web_{hashlib.sha1(url.encode()).hexdigest()[:10]}"


@pytest.mark.xfail(strict=True, reason="D4: a junção mantém a cotação velha antes da do dia")
async def test_d4_repair_with_todays_rate_comes_before_the_stale_one(tmp_path: Path) -> None:
    """Reproduz o e07 da rodada 2: a primeira tentativa comprova uma cotação de 3 dias antes; o
    verificador cobra a de hoje; o reparo a encontra. A do dia vem primeiro."""
    question = "What's the euro exchange rate today?"
    search = FakeSearch(
        [
            WebResult(title="Xe EUR/BRL", url=XE_URL, content=STALE),
            WebResult(title="Google Finance EUR/BRL", url=GOOGLE_URL, content=TODAY),
        ]
    )
    turns = [
        _plan(question, scope="web"),
        tool_call_turn("web_search", query="euro real cotação"),
        final_turn(kout(STALE, claim(_web_id(XE_URL), STALE, statement=f"Xe: {STALE}."))),
        final_turn(verdict(True, uncovered=["today's EUR/BRL rate"])),
        tool_call_turn("web_search", query="EUR BRL hoje"),
        final_turn(kout(TODAY, claim(_web_id(GOOGLE_URL), TODAY, statement=f"Google: {TODAY}."))),
        final_turn(verdict(True)),
    ]
    deps = await make_deps(tmp_path, ScriptedLLM(turns), search=search, max_replans=1)
    response = await handle_chat(authorized(question), deps)
    assert response.plan_changes == ["knowledge_repair"]
    assert TODAY in response.answer
    if STALE in response.answer:
        assert response.answer.index(TODAY) < response.answer.index(STALE)
    assert response.answer.count("Based on the sources consulted") <= 1


@pytest.mark.xfail(strict=True, reason="D5: pendência repete a lacuna e termina com ponto duplo")
async def test_d5_pending_does_not_repeat_the_gap_nor_end_with_double_period(
    tmp_path: Path,
) -> None:
    question = "O que é a Get Smart e como resolvo o erro ADQ 1-19?"
    gap = "como resolver o erro ADQ 1-19"
    turns = [
        _plan(question),
        tool_call_turn("retrieve_getnet_knowledge", query="Get Smart"),
        final_turn(
            kout(
                "A Get Smart tem tela touch.",
                claim(SMART_CHUNK_ID, SMART_QUOTE, statement="A Get Smart tem tela touch."),
                missing(gap),
                pending=[f"{gap.capitalize()}.", "Confirmar o modelo da maquininha."],
            )
        ),
        final_turn(verdict(True, uncovered=[gap])),
    ]
    deps = await make_deps(tmp_path, ScriptedLLM(turns))
    response = await handle_chat(authorized(question), deps)
    assert response.status == "partial"
    assert ".." not in response.answer
    assert response.answer.lower().count(gap.lower()) == 1  # só a nota de lacuna
    assert "Pendências: Confirmar o modelo da maquininha." in response.answer
