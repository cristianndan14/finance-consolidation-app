"""Tests de la verificacion del JWT.

Es la funcion de la que depende todo el aislamiento: el `sub` que sale de aca es
el que se le declara a Postgres. Los casos negativos importan mas que el positivo
— cada uno de ellos, si pasara, seria una forma de hacerse pasar por otro usuario.

Las claves se generan en el test y el JWKS se sirve con un transporte mockeado de
httpx, asi que el camino que se ejercita es el real (fetch, parseo del JWKS,
seleccion por `kid`) sin salir a la red.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.security import jwt as jwt_verify
from app.settings import Settings

SUPABASE_URL = "https://project.supabase.test"
ISSUER = f"{SUPABASE_URL}/auth/v1"
KID = "test-key-1"


def _settings(**overrides: Any) -> Settings:
    # `_env_file=None` para que el .env del repo no se filtre al test.
    return Settings(_env_file=None, supabase_url=SUPABASE_URL, **overrides)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_document(rsa_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    public_jwk: dict[str, Any] = json.loads(
        pyjwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key())
    )
    public_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return {"keys": [public_jwk]}


class JWKSServer:
    """JWKS servido por un transporte mockeado, contando los fetches."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.fetches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        return httpx.Response(200, json=self.document)


@pytest.fixture
def jwks_server(jwks_document: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> JWKSServer:
    server = JWKSServer(jwks_document)
    real_client = httpx.AsyncClient

    def client_with_mock(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(server.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(jwt_verify.httpx, "AsyncClient", client_with_mock)
    jwt_verify.reset_jwks_cache()
    yield server
    jwt_verify.reset_jwks_cache()


def _token(
    key: Any,
    *,
    algorithm: str = "RS256",
    kid: str | None = KID,
    claims: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "aud": "authenticated",
        "role": "authenticated",
        "iss": ISSUER,
        "email": "alguien@test.local",
        "session_id": str(uuid.uuid4()),
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    payload.update(claims or {})
    headers = {"kid": kid} if kid else None
    return pyjwt.encode(payload, key, algorithm=algorithm, headers=headers)


class TestTokenValido:
    async def test_un_token_bien_firmado_devuelve_sus_claims(
        self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer
    ) -> None:
        token = _token(rsa_key)
        verified = await jwt_verify.verify_token(token, _settings())

        assert verified.user_id == pyjwt.decode(token, options={"verify_signature": False})["sub"]
        assert verified.email == "alguien@test.local"
        assert verified.session_id
        assert verified.expires_at > int(time.time())

    async def test_el_jwks_se_busca_una_sola_vez(
        self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer
    ) -> None:
        """Un fetch por request ataria cada pagina a la latencia de Supabase."""
        settings = _settings()
        for _ in range(3):
            await jwt_verify.verify_token(_token(rsa_key), settings)

        assert jwks_server.fetches == 1


class TestTokenRechazado:
    """Cada uno de estos casos, si pasara, seria suplantacion de identidad."""

    async def test_expirado(self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer) -> None:
        token = _token(rsa_key, claims={"exp": int(time.time()) - 60})
        with pytest.raises(jwt_verify.TokenError, match="expirado"):
            await jwt_verify.verify_token(token, _settings())

    async def test_firmado_con_otra_clave(self, jwks_server: JWKSServer) -> None:
        impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(jwt_verify.TokenError):
            await jwt_verify.verify_token(_token(impostor), _settings())

    async def test_de_otro_proyecto_de_supabase(
        self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer
    ) -> None:
        token = _token(rsa_key, claims={"iss": "https://otro.supabase.co/auth/v1"})
        with pytest.raises(jwt_verify.TokenError):
            await jwt_verify.verify_token(token, _settings())

    async def test_con_audiencia_equivocada(
        self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer
    ) -> None:
        """Un token de `anon` no habilita nada: el `aud` tiene que ser authenticated."""
        token = _token(rsa_key, claims={"aud": "anon", "role": "anon"})
        with pytest.raises(jwt_verify.TokenError):
            await jwt_verify.verify_token(token, _settings())

    async def test_con_rol_de_service_role(
        self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer
    ) -> None:
        """`role` es lo que se propaga a Postgres: solo se acepta authenticated."""
        token = _token(rsa_key, claims={"role": "service_role"})
        with pytest.raises(jwt_verify.TokenError, match="rol inesperado"):
            await jwt_verify.verify_token(token, _settings())

    async def test_sin_exp(self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer) -> None:
        """Un token sin vencimiento seria una credencial eterna."""
        payload = {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "role": "authenticated",
            "iss": ISSUER,
        }
        token = pyjwt.encode(payload, rsa_key, algorithm="RS256", headers={"kid": KID})
        with pytest.raises(jwt_verify.TokenError):
            await jwt_verify.verify_token(token, _settings())

    async def test_alg_none(self, jwks_server: JWKSServer) -> None:
        """El ataque de libro: un token sin firma y `alg: none`."""
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "aud": "authenticated",
                "role": "authenticated",
                "iss": ISSUER,
                "exp": int(time.time()) + 60,
            },
            key=None,  # type: ignore[arg-type]
            algorithm="none",
        )
        with pytest.raises(jwt_verify.TokenError, match="algoritmo"):
            await jwt_verify.verify_token(token, _settings())

    async def test_hs256_sin_secreto_configurado(self, jwks_server: JWKSServer) -> None:
        """La confusion de algoritmos: firmar con HS256 usando algo publico.

        Sin `SUPABASE_JWT_SECRET`, HS256 no tiene con que verificarse y el token
        se rechaza antes de mirar la firma.
        """
        token = _token("clave-cualquiera-de-32-bytes-o-mas-x", algorithm="HS256", kid=None)
        with pytest.raises(jwt_verify.TokenError, match="SUPABASE_JWT_SECRET"):
            await jwt_verify.verify_token(token, _settings())

    async def test_sin_kid(self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer) -> None:
        with pytest.raises(jwt_verify.TokenError, match="kid"):
            await jwt_verify.verify_token(_token(rsa_key, kid=None), _settings())

    async def test_malformado(self, jwks_server: JWKSServer) -> None:
        with pytest.raises(jwt_verify.TokenError, match="malformado"):
            await jwt_verify.verify_token("no-es-un-jwt", _settings())


class TestRotacionDeClaves:
    async def test_un_kid_desconocido_refresca_el_jwks(
        self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer
    ) -> None:
        """Cuando Supabase rota la clave, los tokens nuevos traen otro `kid`."""
        settings = _settings()
        await jwt_verify.verify_token(_token(rsa_key), settings)
        assert jwks_server.fetches == 1

        rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        rotated_jwk: dict[str, Any] = json.loads(
            pyjwt.algorithms.RSAAlgorithm.to_jwk(rotated.public_key())
        )
        rotated_jwk.update({"kid": "test-key-2", "alg": "RS256", "use": "sig"})
        jwks_server.document = {"keys": [rotated_jwk]}

        # El refetch por `kid` desconocido tiene un piso de tiempo para no
        # convertirse en un amplificador de trafico; el test lo saltea.
        cache = jwt_verify.get_jwks_cache(settings)
        cache._fetched_at = time.monotonic() - jwt_verify.MIN_REFETCH_SECONDS - 1

        verified = await jwt_verify.verify_token(_token(rotated, kid="test-key-2"), settings)
        assert verified.user_id
        assert jwks_server.fetches == 2

    async def test_un_kid_inventado_no_dispara_un_fetch_por_request(
        self, rsa_key: rsa.RSAPrivateKey, jwks_server: JWKSServer
    ) -> None:
        """Si no, mandar tokens basura seria un ataque de amplificacion gratis."""
        settings = _settings()
        await jwt_verify.verify_token(_token(rsa_key), settings)

        for _ in range(5):
            with pytest.raises(jwt_verify.TokenError):
                await jwt_verify.verify_token(_token(rsa_key, kid="inventado"), settings)

        assert jwks_server.fetches == 1


class TestHS256Local:
    """El Supabase local todavia firma con un secreto compartido."""

    async def test_token_hs256_valido(self) -> None:
        secret = "secreto-del-supabase-local-de-32-o-mas-bytes"
        settings = _settings(supabase_jwt_secret=secret)
        verified = await jwt_verify.verify_token(
            _token(secret, algorithm="HS256", kid=None), settings
        )
        assert verified.user_id

    async def test_token_hs256_con_otro_secreto(self) -> None:
        settings = _settings(supabase_jwt_secret="el-correcto-y-largo-de-mas-de-32-bytes")
        with pytest.raises(jwt_verify.TokenError):
            await jwt_verify.verify_token(
                _token("el-otro-igual-de-largo-pero-distinto-x", algorithm="HS256", kid=None),
                settings,
            )
