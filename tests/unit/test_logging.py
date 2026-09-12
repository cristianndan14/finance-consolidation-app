"""Tests de la configuracion de logging.

Existen por un bug real: `configure_logging` solo se llamaba desde el lifespan, y
como ningun test lo ejecutaba, la app fallaba al arrancar en el primer log. Un
logging roto no se nota en los tests y se nota en el arranque, que es el peor
momento.

Lo otro que se verifica es la redaccion, que aca es una propiedad de seguridad y
no una comodidad: `full_text` tiene todos los consumos de una persona y una URL
firmada de Storage es una credencial que baja el resumen sin autenticarse.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from app.logging_config import REDACT_KEYS, configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Deja el logging como estaba: es estado global del proceso."""
    previous = logging.getLogger().handlers[:]
    yield
    structlog.reset_defaults()
    logging.getLogger().handlers = previous


class TestArranque:
    """El caso que se escapo: configurar y loguear, sin explotar."""

    @pytest.mark.parametrize("json_logs", [False, True])
    def test_se_puede_loguear_despues_de_configurar(
        self, json_logs: bool, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", json_logs=json_logs)

        get_logger("app.smoke").info("aplicacion iniciada", env="test")

        assert "aplicacion iniciada" in capsys.readouterr().err

    def test_el_nombre_del_logger_sale_en_la_linea(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Con el factory equivocado, esto es justamente lo que rompia."""
        configure_logging(level="INFO", json_logs=True)

        get_logger("app.infra.db").info("conectado")

        assert json.loads(capsys.readouterr().err)["logger"] == "app.infra.db"

    def test_los_logs_de_las_librerias_salen_con_el_mismo_formato(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """uvicorn y sqlalchemy no usan structlog: tienen que pasar igual por la
        cadena, o la redaccion no los cubriria."""
        configure_logging(level="INFO", json_logs=True)

        logging.getLogger("uvicorn.error").warning("escuchando en el puerto")

        payload = json.loads(capsys.readouterr().err)
        assert payload["logger"] == "uvicorn.error"
        assert payload["level"] == "warning"

    def test_el_nivel_se_respeta(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="WARNING", json_logs=True)

        get_logger("app.smoke").info("no deberia salir")
        get_logger("app.smoke").warning("esto si")

        assert "no deberia salir" not in capsys.readouterr().err


class TestRedaccion:
    def test_el_texto_del_resumen_no_se_loguea(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", json_logs=True)

        get_logger("app.smoke").info(
            "texto extraido", full_text="COMPRA SUPERMERCADO 45.320,15", pages=3
        )

        payload = json.loads(capsys.readouterr().err)
        assert "SUPERMERCADO" not in json.dumps(payload)
        assert payload["full_text"].startswith("<redacted:")
        # Lo que no es sensible sigue estando: un log sin datos no sirve.
        assert payload["pages"] == 3

    def test_la_url_firmada_no_se_loguea(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Quien tenga esa URL baja el PDF sin autenticarse."""
        configure_logging(level="INFO", json_logs=True)

        get_logger("app.smoke").info(
            "job tomado", download_url="https://proyecto.supabase.co/storage/v1/object/sign/x?t=y"
        )

        assert "supabase.co" not in capsys.readouterr().err

    def test_la_redaccion_alcanza_a_los_logs_de_las_librerias(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", json_logs=True)

        logging.getLogger("otra.libreria").warning("algo", extra={"access_token": "eyJhbGciOi"})

        assert "eyJhbGciOi" not in capsys.readouterr().err

    def test_las_credenciales_estan_en_la_lista(self) -> None:
        """La lista es la defensa: un nombre nuevo de credencial hay que agregarlo."""
        for key in ("password", "access_token", "refresh_token", "download_url", "full_text"):
            assert key in REDACT_KEYS
