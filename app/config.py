"""Configuração da aplicação, lida do ambiente / `.env`. Nenhum valor secreto é logado."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# relativo ao pacote, não ao diretório atual: a API e a ingestão rodam de qualquer lugar
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    openai_chat_model: str = ""
    # vazio = não envia. Modelos de raciocínio (ex.: gpt-6-luna) exigem "none" para usar ferramentas
    openai_reasoning_effort: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    chroma_path: str = "chroma_data"
    timezone: str = "America/Sao_Paulo"

    # produção por padrão: o emissor local de tokens só existe se o ambiente pedir explicitamente
    app_env: Literal["development", "production"] = "production"
    auth_audience: str = "getnet-support-api"
    auth_issuer: str = ""
    auth_jwks_url: str = ""

    state_path: str = "state_data/state.db"
    # retenção configurável; não é um prazo regulatório validado
    conversation_retention_hours: int = 72
    pending_choice_minutes: int = 15
    handoff_ttl_hours: int = 24
    resume_worker_enabled: bool = True

    max_llm_calls: int = 10
    max_tool_calls: int = 5
    max_web_searches: int = 2
    # replanejamento (T07): uma mudança por pedido, só se sobrar orçamento para concluí-la
    max_replans: int = 1
    replan_min_llm_calls: int = 3
    replan_min_seconds: float = 15.0
    deadline_seconds: float = 40.0
