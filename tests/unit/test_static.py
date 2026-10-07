"""Tests de los archivos estaticos: el mount, el COPY de Docker y el cache-bust.

El mount de `/static` no depende de la DB ni de la sesion, asi que va contra la app
real por `ASGITransport` (sin lifespan), igual que `test_auth_routes.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.settings import reset_settings_cache
from app.web.templates import STATIC_DIR, compute_static_version, hash_contents, templates

ROOT = Path(__file__).resolve().parent.parent.parent
AREA_FILES = ["dashboard", "review", "docs", "secondary"]


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("SESSION_SECRET", "un-secreto-de-mas-de-treinta-y-dos-caracteres")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.test")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key-de-prueba")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "secreto-compartido-del-supabase-local")
    reset_settings_cache()
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client
    reset_settings_cache()


@pytest.mark.parametrize("path", ["/static/app.css", "/static/css/review.css"])
async def test_static_css_se_sirve(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_dockerfile_copia_static() -> None:
    assert "COPY static ./static" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_static_version_no_vacio_y_cambia_con_el_contenido() -> None:
    assert templates.env.globals["static_version"]
    assert hash_contents([b"a{}"]) != hash_contents([b"b{}"])
    assert hash_contents([b"a{}"]) == hash_contents([b"a{}"])
    assert len(hash_contents([b"a{}"])) == 10


def test_static_version_dev_si_falta_la_carpeta(tmp_path: Path) -> None:
    assert compute_static_version(tmp_path / "no-existe") == "dev"


def test_static_version_cambia_al_editar_un_css(tmp_path: Path) -> None:
    (tmp_path / "css").mkdir()
    (tmp_path / "app.css").write_text("a{}", encoding="utf-8")
    antes = compute_static_version(tmp_path)
    (tmp_path / "css" / "review.css").write_text(".rv-x{}", encoding="utf-8")
    assert compute_static_version(tmp_path) != antes


@pytest.mark.parametrize("name", AREA_FILES)
def test_hay_un_css_por_area(name: str) -> None:
    assert (STATIC_DIR / "css" / f"{name}.css").is_file()
