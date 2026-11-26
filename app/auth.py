"""Autenticação: valida o access token e constrói o principal no servidor (T02).

O token diz *quem* é o chamador (sub, tenant, papéis, escopos). *Quais estabelecimentos* ele
acessa é resolvido aqui, por um repositório do servidor, nunca por um campo do token ou do
payload. Os algoritmos aceitos são fixos e a chave vem do emissor configurado, nunca de um
endereço indicado pelo próprio token.
"""

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import DATA_DIR, Settings

ALGORITHMS = ["RS256"]
DEV_ISSUER = "urn:getnet-support:dev-issuer"
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "iss", "aud"]
_LEEWAY_SECONDS = 30
PRINCIPALS_FILE = DATA_DIR / "fixtures" / "principals.json"


class AuthenticationError(Exception):
    """Token ausente, inválido, expirado ou de emissor/audiência incorretos (401)."""


@dataclass(frozen=True)
class Principal:
    sub: str
    tenant_id: str
    roles: frozenset[str]
    scopes: frozenset[str]
    customer_ids: frozenset[str]


class AccessRepository(Protocol):
    def customers_for(self, tenant_id: str, sub: str) -> frozenset[str]: ...


class KeyResolver(Protocol):
    async def key_for(self, token: str) -> Any: ...


class JsonAccessRepository:
    """Mapa de acesso de desenvolvimento: principal → estabelecimentos. Em produção, outra
    implementação consulta o cadastro real."""

    def __init__(self, path: str | Path = PRINCIPALS_FILE) -> None:
        raw = json.loads(Path(path).read_text("utf-8"))
        self.tenant_id: str = raw["tenant_id"]
        self.profiles: dict[str, dict[str, list[str]]] = raw["principals"]

    def customers_for(self, tenant_id: str, sub: str) -> frozenset[str]:
        if tenant_id != self.tenant_id or sub not in self.profiles:
            return frozenset()
        return frozenset(self.profiles[sub].get("customers", []))


class StaticKey:
    def __init__(self, key: Any) -> None:
        self._key = key

    async def key_for(self, token: str) -> Any:
        return self._key


class JwksKeys:
    """Chaves do emissor configurado (JWKS), com cache e rotação pelo `kid`."""

    def __init__(self, url: str, cache_seconds: int = 300) -> None:
        self._client = jwt.PyJWKClient(url, cache_keys=True, lifespan=cache_seconds)

    async def key_for(self, token: str) -> Any:
        try:
            signing_key = await asyncio.to_thread(self._client.get_signing_key_from_jwt, token)
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"signing key unavailable: {type(exc).__name__}") from exc
        return signing_key.key


class DevIssuer:
    """Emissor local para desenvolvimento e testes, com chave efêmera (gerada no processo).
    A API valida os tokens dele pelo mesmo caminho criptográfico usado em produção."""

    def __init__(self, audience: str, profiles: dict[str, dict[str, list[str]]], tenant_id: str):
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._kid = f"dev-{secrets.token_hex(4)}"
        self.audience = audience
        self.profiles = profiles
        self.tenant_id = tenant_id

    @property
    def public_key(self) -> Any:
        return self._key.public_key()

    def issue(
        self,
        sub: str,
        *,
        scopes: list[str] | None = None,
        ttl_seconds: int = 900,
        **overrides: Any,
    ) -> str:
        """Token para um principal de desenvolvimento. `scopes` só pode reduzir os escopos do
        perfil; `overrides` existe para os testes produzirem tokens inválidos."""
        profile = self.profiles.get(sub, {})
        allowed = profile.get("scopes", [])
        granted = allowed if scopes is None else [s for s in scopes if s in allowed]
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": DEV_ISSUER,
            "aud": self.audience,
            "sub": sub,
            "tenant_id": self.tenant_id,
            "roles": profile.get("roles", []),
            "scope": " ".join(granted),
            "iat": now,
            "exp": now + ttl_seconds,
        }
        claims.update(overrides)
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self._kid})


class Authenticator:
    def __init__(
        self, keys: KeyResolver, *, issuer: str, audience: str, access: AccessRepository
    ) -> None:
        self._keys = keys
        self._issuer = issuer
        self._audience = audience
        self._access = access

    @property
    def access(self) -> AccessRepository:
        """Mapa de acesso vigente: a retomada de um caso revalida o cliente por ele."""
        return self._access

    async def authenticate(self, token: str) -> Principal:
        key = await self._keys.key_for(token)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=_LEEWAY_SECONDS,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(type(exc).__name__) from exc
        tenant_id = claims.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise AuthenticationError("missing tenant_id")
        scope = claims.get("scope", "")
        roles = claims.get("roles", [])
        if not isinstance(scope, str) or not isinstance(roles, list):
            raise AuthenticationError("malformed scope or roles")
        sub = str(claims["sub"])
        return Principal(
            sub=sub,
            tenant_id=tenant_id,
            roles=frozenset(str(r) for r in roles),
            scopes=frozenset(scope.split()),
            customer_ids=self._access.customers_for(tenant_id, sub),
        )


def build_auth(
    settings: Settings, access: JsonAccessRepository | None = None
) -> tuple[Authenticator, DevIssuer | None]:
    """Desenvolvimento: emissor local efêmero. Produção: exige emissor e JWKS configurados e
    recusa o emissor de desenvolvimento."""
    access = access or JsonAccessRepository()
    if settings.app_env == "development":
        issuer = DevIssuer(settings.auth_audience, access.profiles, access.tenant_id)
        authenticator = Authenticator(
            StaticKey(issuer.public_key),
            issuer=DEV_ISSUER,
            audience=settings.auth_audience,
            access=access,
        )
        return authenticator, issuer
    if not settings.auth_issuer or settings.auth_issuer == DEV_ISSUER:
        raise RuntimeError("AUTH_ISSUER must name a trusted issuer in production")
    if not settings.auth_jwks_url.startswith("https://"):
        raise RuntimeError("AUTH_JWKS_URL must be an https URL in production")
    authenticator = Authenticator(
        JwksKeys(settings.auth_jwks_url),
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
        access=access,
    )
    return authenticator, None
