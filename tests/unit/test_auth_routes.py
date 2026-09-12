"""Tests de las rutas de login/logout y del refresco de sesion.

Van contra la app real por `ASGITransport` (sin levantar un servidor y sin correr
el lifespan, asi que no hace falta Postgres): lo que se ejercita es el camino
completo de middleware, dependencias y handlers de error. Supabase Auth se
reemplaza por funciones falsas — lo que importa aca es lo que hace el backend con
la respuesta, no GoTrue.

Los tokens se firman con HS256, que es lo que hace el Supabase local.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from starlette.requests import Request

from app import deps
from app.infra import supabase_auth
from app.security import csrf
from app.security import session as session_store
from app.security.exceptions import NotAuthenticatedError
from app.settings import Settings, get_settings, reset_settings_cache

SUPABASE_URL = "https://project.supabase.test"
JWT_SECRET = "secreto-compartido-del-supabase-local"
SESSION_SECRET = "un-secreto-de-mas-de-treinta-y-dos-caracteres"
USER_ID = str(uuid.uuid4())
EMAIL = "alguien@test.local"

HTML = {"accept": "text/html,application/xhtml+xml"}


def _access_token(*, user_id: str = USER_ID, ttl: int = 3600) -> str:
    return pyjwt.encode(
        {
            "sub": user_id,
            "aud": "authenticated",
            "role": "authenticated",
            "iss": f"{SUPABASE_URL}/auth/v1",
            "email": EMAIL,
            "session_id": str(uuid.uuid4()),
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl,
        },
        JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings del proceso, apuntadas al proyecto ficticio del test.

    Las variables de entorno pisan al `.env` del repo, asi que el test no depende
    de como este configurada la maquina.
    """
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key-de-prueba")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
    reset_settings_cache()
    yield get_settings()
    reset_settings_cache()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client


def _session_cookie(settings: Settings, *, ttl: int = 3600, token: str | None = None) -> str:
    session = session_store.Session(
        user_id=USER_ID,
        email=EMAIL,
        access_token=token or _access_token(ttl=ttl),
        refresh_token="refresh-token-valor",
        access_expires_at=int(time.time()) + ttl,
    )
    return session_store.encode(session, settings)


def _fake_tokens(**overrides: Any) -> supabase_auth.TokenPair:
    data: dict[str, Any] = {
        "access_token": _access_token(),
        "refresh_token": "refresh-nuevo",
        "expires_at": int(time.time()) + 3600,
        "user_id": USER_ID,
        "email": EMAIL,
    }
    data.update(overrides)
    return supabase_auth.TokenPair(**data)


class TestLogin:
    async def test_el_formulario_se_muestra(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/login", headers=HTML)
        assert response.status_code == 200
        assert 'name="password"' in response.text

    async def test_credenciales_correctas_dejan_la_cookie(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_sign_in(email: str, password: str, cfg: Any = None) -> Any:
            assert email == EMAIL
            return _fake_tokens()

        monkeypatch.setattr(supabase_auth, "sign_in_with_password", fake_sign_in)

        response = await client.post(
            "/login", data={"email": EMAIL, "password": "correcta"}, headers=HTML
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "HttpOnly" in response.headers["set-cookie"]

    async def test_el_email_se_normaliza(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recibido: list[str] = []

        async def fake_sign_in(email: str, password: str, cfg: Any = None) -> Any:
            recibido.append(email)
            return _fake_tokens()

        monkeypatch.setattr(supabase_auth, "sign_in_with_password", fake_sign_in)
        await client.post("/login", data={"email": f"  {EMAIL} ", "password": "x"}, headers=HTML)

        assert recibido == [EMAIL]

    async def test_credenciales_malas_no_dejan_cookie(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_sign_in(email: str, password: str, cfg: Any = None) -> Any:
            raise supabase_auth.AuthError("email o contraseña incorrectos", status=400)

        monkeypatch.setattr(supabase_auth, "sign_in_with_password", fake_sign_in)

        response = await client.post(
            "/login", data={"email": EMAIL, "password": "mala"}, headers=HTML
        )

        assert response.status_code == 200
        assert "incorrectos" in response.text
        assert "set-cookie" not in response.headers

    async def test_el_next_a_otro_sitio_se_ignora(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un `next` sin filtrar convierte el login en un redirector abierto."""
        monkeypatch.setattr(
            supabase_auth,
            "sign_in_with_password",
            lambda *a, **k: _async(_fake_tokens()),
        )

        response = await client.post(
            "/login",
            data={"email": EMAIL, "password": "x", "next": "https://sitio-falso.test/"},
            headers=HTML,
        )

        assert response.headers["location"] == "/"

    async def test_el_next_relativo_se_respeta(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            supabase_auth,
            "sign_in_with_password",
            lambda *a, **k: _async(_fake_tokens()),
        )

        response = await client.post(
            "/login",
            data={"email": EMAIL, "password": "x", "next": "/admin/invitations"},
            headers=HTML,
        )

        assert response.headers["location"] == "/admin/invitations"


async def _async(value: Any) -> Any:
    return value


class TestRutasProtegidas:
    async def test_sin_cookie_va_al_login(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/", headers=HTML)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    async def test_con_cookie_adulterada_va_al_login(
        self, client: httpx.AsyncClient, settings: Settings
    ) -> None:
        raw = _session_cookie(settings)
        client.cookies.set(settings.session_cookie_name, raw[:-4] + "AAAA")

        response = await client.get("/", headers=HTML)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    async def test_una_ruta_profunda_vuelve_a_donde_iba(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/admin/invitations", headers=HTML)
        assert response.headers["location"] == "/login?next=/admin/invitations"

    async def test_htmx_recibe_401_y_no_una_redireccion(self, client: httpx.AsyncClient) -> None:
        """Un 303 al login se le inyectaria a HTMX dentro de la pagina."""
        response = await client.get("/", headers={**HTML, "HX-Request": "true"})

        assert response.status_code == 401
        assert response.headers["HX-Redirect"] == "/login"

    async def test_un_token_de_otro_proyecto_borra_la_cookie(
        self, client: httpx.AsyncClient, settings: Settings
    ) -> None:
        """La cookie esta bien firmada, pero el token que lleva no verifica."""
        ajeno = pyjwt.encode(
            {
                "sub": USER_ID,
                "aud": "authenticated",
                "role": "authenticated",
                "iss": "https://otro-proyecto.supabase.co/auth/v1",
                "exp": int(time.time()) + 3600,
            },
            JWT_SECRET,
            algorithm="HS256",
        )
        client.cookies.set(settings.session_cookie_name, _session_cookie(settings, token=ajeno))

        response = await client.get("/", headers=HTML)

        assert response.status_code == 303
        # La cookie muerta se borra: si no, cada click repetiria el mismo error.
        assert 'fc_session=""' in response.headers["set-cookie"]


class TestLogout:
    async def test_sin_token_csrf_no_cierra_la_sesion(
        self, client: httpx.AsyncClient, settings: Settings
    ) -> None:
        client.cookies.set(settings.session_cookie_name, _session_cookie(settings))

        response = await client.post("/logout", headers=HTML)

        assert response.status_code == 403
        assert "set-cookie" not in response.headers

    async def test_con_token_csrf_borra_la_cookie(
        self, client: httpx.AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        revocados: list[str] = []

        async def fake_sign_out(access_token: str, cfg: Any = None) -> None:
            revocados.append(access_token)

        monkeypatch.setattr(supabase_auth, "sign_out", fake_sign_out)
        client.cookies.set(settings.session_cookie_name, _session_cookie(settings))

        response = await client.post(
            "/logout", data={"csrf_token": csrf.issue(USER_ID, settings)}, headers=HTML
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert 'fc_session=""' in response.headers["set-cookie"]
        assert len(revocados) == 1, "la sesion tambien se revoca del lado de Supabase"

    async def test_el_csrf_de_otra_sesion_no_sirve(
        self, client: httpx.AsyncClient, settings: Settings
    ) -> None:
        client.cookies.set(settings.session_cookie_name, _session_cookie(settings))

        response = await client.post(
            "/logout", data={"csrf_token": csrf.issue(str(uuid.uuid4()), settings)}, headers=HTML
        )

        assert response.status_code == 403


class TestRefrescoDeTokens:
    """El refresh vive en la dependencia, que es el unico lugar que ve la sesion."""

    def _request(self) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
                "state": {},
            }
        )

    async def test_un_token_por_vencer_se_renueva_y_deja_cookie_nueva(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Supabase rota el refresh token: si la cookie no se actualiza, el
        proximo request queda con un refresh token ya consumido."""

        async def fake_refresh(refresh_token: str, cfg: Any = None) -> Any:
            assert refresh_token == "refresh-token-valor"
            return _fake_tokens(refresh_token="refresh-rotado")

        monkeypatch.setattr(supabase_auth, "refresh_session", fake_refresh)

        expirando = session_store.Session(
            user_id=USER_ID,
            email=EMAIL,
            access_token=_access_token(ttl=10),
            refresh_token="refresh-token-valor",
            access_expires_at=int(time.time()) + 10,
        )
        request = self._request()

        user = await deps.current_user(request, expirando, settings)

        assert user.user_id == USER_ID
        renewed = getattr(request.state, deps.SESSION_UPDATE_ATTR)
        assert renewed.refresh_token == "refresh-rotado"

    async def test_si_el_refresh_falla_hay_que_volver_a_loguearse(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_refresh(refresh_token: str, cfg: Any = None) -> Any:
            raise supabase_auth.AuthError("la sesion expiro", status=400)

        monkeypatch.setattr(supabase_auth, "refresh_session", fake_refresh)

        vencida = session_store.Session(
            user_id=USER_ID,
            email=EMAIL,
            access_token=_access_token(ttl=-60),
            refresh_token="refresh-token-valor",
            access_expires_at=int(time.time()) - 60,
        )

        with pytest.raises(NotAuthenticatedError) as caught:
            await deps.current_user(self._request(), vencida, settings)

        assert caught.value.clear_cookie is True

    async def test_una_cookie_con_el_usuario_cambiado_corta(self, settings: Settings) -> None:
        """El token manda: si la cookie dice otro usuario, algo mezclo sesiones."""
        mezclada = session_store.Session(
            user_id=str(uuid.uuid4()),
            email=EMAIL,
            access_token=_access_token(),
            refresh_token="refresh-token-valor",
            access_expires_at=int(time.time()) + 3600,
        )

        with pytest.raises(NotAuthenticatedError):
            await deps.current_user(self._request(), mezclada, settings)
