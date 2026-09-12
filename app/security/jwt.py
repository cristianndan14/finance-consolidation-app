"""Verificacion del JWT que emite Supabase.

# Por que se verifica y no se decodifica

El backend usa el `sub` del token para decirle a Postgres quien es el usuario
(ver `app/infra/db.py`), y Postgres decide con eso que filas se ven. Un token
decodificado sin verificar es un `sub` elegido por quien manda la request: seria
tan fuerte como pedirle al cliente que declare su identidad. Toda la cadena de
aislamiento se apoya en esta funcion.

# Que se valida

- **Firma**, con la clave publica del proyecto (JWKS) o con el secreto compartido
  si el token viene firmado con HS256 (el Supabase local todavia lo hace).
- **`alg`** contra una lista blanca. Sin esto, un token con `alg: none` pasaria,
  o uno con `alg: HS256` firmado con la clave publica del proyecto, que es publica.
- **`exp`** con un leeway chico para el desfasaje de reloj.
- **`iss`** contra el proyecto configurado: un token valido de otro proyecto de
  Supabase no sirve aca.
- **`aud`** y `role` = `authenticated`: descarta tokens de `anon` y `service_role`.

# Cache de JWKS

Las claves se cachean con TTL de una hora: un fetch por request agregaria una
llamada de red al camino critico y ataria cada request a la disponibilidad del
endpoint. Un `kid` desconocido fuerza un refetch (es lo que pasa cuando Supabase
rota la clave), pero con un piso de tiempo entre refetches para que tokens basura
no se conviertan en un amplificador de trafico contra Supabase.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx
import jwt
from jwt import PyJWK, PyJWKSet

from app.logging_config import get_logger
from app.settings import Settings, get_settings

log = get_logger(__name__)

# HS256 esta solo por el Supabase local. Los proyectos hosted nuevos firman con
# ES256; RS256 queda para los creados antes del cambio.
ALLOWED_ALGORITHMS: Final = frozenset({"ES256", "RS256", "HS256"})

# Supabase pone `authenticated` en el `aud` de los tokens de usuario.
EXPECTED_AUDIENCE: Final = "authenticated"

# Desfasaje de reloj tolerado en `exp` / `iat`.
CLOCK_LEEWAY_SECONDS: Final = 10

# Piso entre refetches de JWKS ante un `kid` desconocido.
MIN_REFETCH_SECONDS: Final = 60

JWKS_TIMEOUT_SECONDS: Final = 5.0


class TokenError(Exception):
    """El token no es utilizable. El motivo va al log, no a la respuesta HTTP."""


@dataclass(frozen=True)
class VerifiedToken:
    """Claims ya verificados. Que exista significa que la firma es valida."""

    claims: dict[str, Any]

    @property
    def user_id(self) -> str:
        return str(self.claims["sub"])

    @property
    def email(self) -> str | None:
        value = self.claims.get("email")
        return str(value) if value else None

    @property
    def session_id(self) -> str | None:
        value = self.claims.get("session_id")
        return str(value) if value else None

    @property
    def expires_at(self) -> int:
        return int(self.claims["exp"])


class JWKSCache:
    """Claves publicas del proyecto, cacheadas por `kid`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    def _is_fresh(self) -> bool:
        if self._fetched_at is None:
            return False
        return (time.monotonic() - self._fetched_at) < self._settings.jwks_cache_ttl_seconds

    def _can_refetch(self) -> bool:
        if self._fetched_at is None:
            return True
        return (time.monotonic() - self._fetched_at) >= MIN_REFETCH_SECONDS

    async def get(self, kid: str) -> PyJWK:
        key = self._keys.get(kid)
        if key is not None and self._is_fresh():
            return key

        # El lock evita la estampida: si llegan N requests con la cache vencida,
        # una sola pega a Supabase y las demas usan lo que trajo.
        async with self._lock:
            key = self._keys.get(kid)
            if key is not None and self._is_fresh():
                return key
            if not self._can_refetch():
                if key is not None:
                    return key
                raise TokenError(f"kid desconocido y refetch en cooldown: {kid}")
            await self._refresh()

        key = self._keys.get(kid)
        if key is None:
            raise TokenError(f"el JWKS del proyecto no tiene el kid {kid}")
        return key

    async def _refresh(self) -> None:
        url = self._settings.jwks_url
        try:
            async with httpx.AsyncClient(timeout=JWKS_TIMEOUT_SECONDS) as client:
                response = await client.get(url)
                response.raise_for_status()
                document = response.json()
        except Exception as exc:
            # Se marca el intento igual: si Supabase esta caido, no conviene
            # reintentar en cada request que llegue.
            self._fetched_at = time.monotonic()
            log.error("no se pudo obtener el JWKS", url=url, error=type(exc).__name__)
            raise TokenError("no se pudo obtener el JWKS del proyecto") from exc

        try:
            key_set = PyJWKSet.from_dict(document)
        except Exception as exc:
            self._fetched_at = time.monotonic()
            raise TokenError("el JWKS del proyecto no es valido") from exc

        self._keys = {key.key_id: key for key in key_set.keys if key.key_id}
        self._fetched_at = time.monotonic()
        log.info("JWKS actualizado", keys=len(self._keys))


_cache: JWKSCache | None = None


def get_jwks_cache(settings: Settings | None = None) -> JWKSCache:
    global _cache
    if _cache is None:
        _cache = JWKSCache(settings or get_settings())
    return _cache


def reset_jwks_cache() -> None:
    """Para los tests: la cache no debe cruzar configuraciones."""
    global _cache
    _cache = None


async def _key_for(header: dict[str, Any], settings: Settings) -> Any:
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise TokenError(f"algoritmo de firma no permitido: {algorithm!r}")

    if algorithm == "HS256":
        secret = settings.supabase_jwt_secret.get_secret_value()
        if not secret:
            raise TokenError(
                "el token esta firmado con HS256 pero SUPABASE_JWT_SECRET no esta configurada"
            )
        return secret

    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise TokenError("token con firma asimetrica y sin kid")
    return (await get_jwks_cache(settings).get(kid)).key


async def verify_token(token: str, settings: Settings | None = None) -> VerifiedToken:
    """Verifica firma y claims. Levanta `TokenError` si el token no sirve."""
    cfg = settings or get_settings()

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise TokenError("token malformado") from exc

    key = await _key_for(header, cfg)
    algorithm = str(header["alg"])

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            audience=EXPECTED_AUDIENCE,
            issuer=cfg.jwt_issuer,
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expirado") from exc
    except jwt.PyJWTError as exc:
        raise TokenError(f"token invalido: {type(exc).__name__}") from exc

    # `role` es lo que se propaga como rol de Postgres. Un token de service_role
    # no tiene por que entrar por el camino del usuario.
    if claims.get("role") != EXPECTED_AUDIENCE:
        raise TokenError(f"rol inesperado en el token: {claims.get('role')!r}")

    return VerifiedToken(claims)
