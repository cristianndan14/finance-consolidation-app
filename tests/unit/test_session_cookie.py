"""Tests de la cookie de sesion y del token CSRF.

Lo que se verifica es lo que sostiene la sesion: que el payload no se pueda
modificar, que caduque, y que un token CSRF de una sesion no valga en otra.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from starlette.responses import Response

from app.security import csrf
from app.security import session as session_store
from app.settings import Settings

SECRET = "un-secreto-de-mas-de-treinta-y-dos-caracteres"


def _settings(**overrides: Any) -> Settings:
    overrides.setdefault("session_secret", SECRET)
    return Settings(_env_file=None, **overrides)


def _session(**overrides: Any) -> session_store.Session:
    data: dict[str, Any] = {
        "user_id": "11111111-1111-1111-1111-111111111111",
        "email": "alguien@test.local",
        "access_token": "access.token.valor",
        "refresh_token": "refresh-token-valor",
        "access_expires_at": int(time.time()) + 3600,
    }
    data.update(overrides)
    return session_store.Session(**data)


class TestRoundTrip:
    def test_lo_que_se_firma_se_recupera_igual(self) -> None:
        settings = _settings()
        original = _session()
        recovered = session_store.decode(session_store.encode(original, settings), settings)
        assert recovered == original

    def test_una_cookie_adulterada_no_vale(self) -> None:
        """El caso que importa: cambiar el `sub` para ver los datos de otro."""
        settings = _settings()
        raw = session_store.encode(_session(), settings)
        assert session_store.decode(raw[:-4] + "AAAA", settings) is None

    def test_una_cookie_firmada_con_otro_secreto_no_vale(self) -> None:
        raw = session_store.encode(_session(), _settings())
        assert session_store.decode(raw, _settings(session_secret="otro-" + SECRET)) is None

    def test_una_cookie_vencida_no_vale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Se firma "hace dos semanas y un dia" corriendo el reloj del firmante,
        que es mas honesto que dormir el test hasta que venza."""
        import itsdangerous.timed

        settings = _settings()
        vencida = time.time() - settings.session_max_age_seconds - 86400
        monkeypatch.setattr(itsdangerous.timed.time, "time", lambda: vencida)
        raw = session_store.encode(_session(), settings)
        monkeypatch.undo()

        assert session_store.decode(raw, settings) is None

    def test_un_payload_incompleto_no_explota(self) -> None:
        """Una cookie de una version anterior del formato: se descarta, no rompe."""
        from itsdangerous import URLSafeTimedSerializer

        settings = _settings()
        raw = URLSafeTimedSerializer(SECRET, salt=session_store.SESSION_SALT).dumps({"u": "x"})
        assert session_store.decode(raw, settings) is None

    def test_sin_session_secret_no_se_firma(self) -> None:
        """Firmar con un secreto vacio es no firmar: mejor fallar al arrancar."""
        with pytest.raises(session_store.SessionSecretMissingError):
            session_store.encode(_session(), Settings(_env_file=None, session_secret=""))

    def test_el_salt_de_csrf_no_sirve_para_la_sesion(self) -> None:
        """Mismo secreto, dos propositos: un token de uno no vale en el otro."""
        settings = _settings()
        token = csrf.issue("11111111-1111-1111-1111-111111111111", settings)
        assert session_store.decode(token, settings) is None


class TestRefresco:
    def test_un_token_lejos_de_vencer_no_se_refresca(self) -> None:
        assert not _session(access_expires_at=int(time.time()) + 3600).needs_refresh(120)

    def test_un_token_dentro_del_margen_se_refresca(self) -> None:
        """El margen evita que el token venza entre la validacion y el query."""
        assert _session(access_expires_at=int(time.time()) + 60).needs_refresh(120)

    def test_un_token_ya_vencido_se_refresca(self) -> None:
        assert _session(access_expires_at=int(time.time()) - 10).needs_refresh(120)


class TestCookieAttributes:
    def test_la_cookie_es_httponly_y_samesite_lax(self) -> None:
        """httpOnly es lo que hace que un XSS no pueda leer el access token."""
        response = Response()
        session_store.attach(response, _session(), _settings())
        header = response.headers["set-cookie"]

        assert "fc_session=" in header
        assert "HttpOnly" in header
        assert "SameSite=lax" in header.replace("samesite", "SameSite")
        assert "Secure" not in header  # development: http://localhost

    def test_en_produccion_la_cookie_es_secure(self) -> None:
        response = Response()
        session_store.attach(response, _session(), _settings(session_cookie_secure=True))
        assert "Secure" in response.headers["set-cookie"]

    def test_borrar_la_cookie_usa_los_mismos_atributos(self) -> None:
        """Con atributos distintos, el navegador deja viva la cookie vieja."""
        response = Response()
        session_store.clear(response, _settings())
        header = response.headers["set-cookie"]

        assert "fc_session=" in header
        assert "HttpOnly" in header
        assert "Path=/" in header


class TestCSRF:
    def test_el_token_propio_verifica(self) -> None:
        settings = _settings()
        user = "11111111-1111-1111-1111-111111111111"
        csrf.verify(csrf.issue(user, settings), user, settings)

    def test_el_token_de_otra_sesion_no_verifica(self) -> None:
        """Es el punto del diseño: el token lleva adentro a quien pertenece."""
        settings = _settings()
        token = csrf.issue("11111111-1111-1111-1111-111111111111", settings)
        with pytest.raises(csrf.CSRFError, match="otra sesion"):
            csrf.verify(token, "22222222-2222-2222-2222-222222222222", settings)

    def test_sin_token_falla(self) -> None:
        with pytest.raises(csrf.CSRFError, match="falta"):
            csrf.verify(None, "11111111-1111-1111-1111-111111111111", _settings())

    def test_un_token_inventado_falla(self) -> None:
        with pytest.raises(csrf.CSRFError):
            csrf.verify("cualquier-cosa", "11111111-1111-1111-1111-111111111111", _settings())
