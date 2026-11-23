"""Logs estruturados (JSON) com allowlist de campos (T09).

Só campos operacionais conhecidos saem: ids técnicos, contagens, duração, códigos e status.
Campo fora da lista é descartado (só o nome vai em `dropped_fields`, para corrigir o chamador),
então texto do cliente, resposta, seletor ou dado de ferramenta não chegam aos logs por engano.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger("getnet.support")

ALLOWED_FIELDS = frozenset(
    {
        "request_id",
        "workflow",
        "status",
        "agent",
        "agents",
        "tool",
        "tool_name",
        "source_ids",
        "index_version",
        "duration_ms",
        "error_code",
        "detail",
        "reason",
        "decision",
        "outcome",
        "replans_used",
        "llm_calls",
        "tool_calls",
        "web_searches",
        "embedding_calls",
        "token_usage",
        "handoff_id",
        "delivered",
        "in_flight",
        "limit",
    }
)
# `detail` vem de mensagens de exceção do próprio código; por garantia, padrões de dado pessoal
# ou financeiro são mascarados antes de sair
_SCRUB = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+|R\$\s?[\d.,]+|\d{8,}|\b[RT]-\d{4}-\d{2,3}\b|cliente\d+",
    re.IGNORECASE,
)


def _clean(key: str, value: Any) -> Any:
    if key == "detail":
        return _SCRUB.sub("[redacted]", str(value))
    return value


def log_event(event: str, **fields: Any) -> None:
    safe = {k: _clean(k, v) for k, v in fields.items() if k in ALLOWED_FIELDS}
    dropped = sorted(k for k in fields if k not in ALLOWED_FIELDS)
    if dropped:
        safe["dropped_fields"] = dropped
    logger.info(json.dumps({"event": event, **safe}, default=str, ensure_ascii=False))


def configure_logging() -> None:
    """Logs JSON em stdout (uma linha por evento), prontos para coleta em container."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
