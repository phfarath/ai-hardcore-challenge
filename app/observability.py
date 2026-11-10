"""Logs estruturados (JSON) sem texto de mensagem/resposta nem identidade do cliente."""

import json
import logging
from typing import Any

logger = logging.getLogger("getnet.support")

# nunca logados, mesmo se um chamador os passar por engano
_FORBIDDEN = frozenset({"message", "answer", "question", "user_id"})


def log_event(event: str, **fields: Any) -> None:
    safe = {k: v for k, v in fields.items() if k not in _FORBIDDEN}
    logger.info(json.dumps({"event": event, **safe}, default=str, ensure_ascii=False))


def configure_logging() -> None:
    """Logs JSON em stdout (uma linha por evento), prontos para coleta em container."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
