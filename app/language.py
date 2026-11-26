"""Idioma dos textos fixos da resposta (notas, cabeçalhos, dicas de reparo).

Detecção simples por palavras funcionais da mensagem do cliente: o sistema atende só português e
inglês, e o texto livre do LLM já segue o idioma da pergunta. Empate ou mensagem sem pistas fica
em português, o idioma padrão do atendimento."""

import re
from typing import Literal

Language = Literal["pt", "en"]

_EN = frozenset(
    re.findall(
        r"\S+",
        "the what what's how my is are do does did can could i i'm to and when will you your "
        "which where why it of in on for with this that there",
    )
)
_PT = frozenset(
    re.findall(
        r"\S+",
        "o os as que não nao minha meu minhas meus como qual quais quando é e de do da dos das "
        "para com uma um por quero posso tem está esta isso vou preciso",
    )
)
_WORD = re.compile(r"[a-zà-ÿ']+")


def detect_language(text: str) -> Language:
    words = _WORD.findall(text.lower())
    english = sum(w in _EN for w in words)
    portuguese = sum(w in _PT for w in words)
    return "en" if english > portuguese else "pt"
