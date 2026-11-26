"""Fronteira de privacidade (T09): o que sai do processo para serviços externos.

Consultas públicas (busca web e o texto da consulta que vira embedding no provedor) não podem
levar dados do cliente. A checagem combina padrões genéricos (e-mail, CPF/CNPJ, valores em
reais, sequências longas de dígitos, ids de registro) com os valores privados que esta própria
requisição já viu: o seletor do cliente e ids e valores devolvidos pelas ferramentas de dados.
Uma consulta recusada não é reescrita: a ferramenta devolve erro e o agente segue sem ela.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.context import RequestContext

_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "document": re.compile(
        r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"
    ),
    "amount": re.compile(r"R\$\s?\d"),
    "long_number": re.compile(r"\d{8,}"),
    "record_id": re.compile(r"\b[RT]-\d{4}-\d{2,3}\b", re.IGNORECASE),
}
_PRIVATE_KEYS = frozenset({"id", "terminal_id", "amount"})


class PrivateDataInQuery(Exception):
    """A consulta destinada a um serviço externo contém dado privado; o motivo é uma categoria,
    nunca o valor encontrado."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _amount_forms(value: str) -> set[str]:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return {value}
    plain = f"{amount:.2f}"
    integer, cents = plain.split(".")
    grouped = f"{int(integer):,}".replace(",", ".")
    return {plain, f"{integer},{cents}", f"{grouped},{cents}"}


def _collect(data: Any, found: set[str]) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in _PRIVATE_KEYS and isinstance(value, str) and value:
                found |= _amount_forms(value) if key == "amount" else {value}
            else:
                _collect(value, found)
    elif isinstance(data, list):
        for item in data:
            _collect(item, found)


def private_values(ctx: RequestContext) -> set[str]:
    """Valores privados conhecidos nesta requisição: cliente e saídas das ferramentas de dados."""
    found: set[str] = {ctx.user_id} if ctx.user_id else set()
    for result in ctx.tool_results:
        if any(e.type == "customer_record" for e in result.evidence):
            _collect(result.data, found)
    return {v for v in found if len(v) >= 3}


def check_public_query(query: str, ctx: RequestContext) -> None:
    """Levanta `PrivateDataInQuery` se a consulta pública carrega dado do cliente."""
    lowered = query.lower()
    if any(value.lower() in lowered for value in private_values(ctx)):
        raise PrivateDataInQuery("request_private_value")
    for reason, pattern in _PATTERNS.items():
        if pattern.search(query):
            raise PrivateDataInQuery(reason)
