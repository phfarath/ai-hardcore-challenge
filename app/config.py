"""Configuração da aplicação, lida do ambiente / `.env`. Nenhum valor secreto é logado."""

from pathlib import Path

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

    max_llm_calls: int = 6
    max_tool_calls: int = 4
    max_web_searches: int = 1
    deadline_seconds: float = 40.0
