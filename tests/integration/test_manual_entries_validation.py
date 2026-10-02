"""Validacion server-side de `/movements` y efectos en el dashboard.

Rutas ejercitadas por HTTP real (ASGI) contra Postgres: lo que se verifica es
que una entrada invalida vuelva con un error amigable (303 a `?error=`) y no
escriba nada, nunca un 500.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import asyncpg
import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio

from app.infra import db
from app.repositories import analytics
from app.repositories import manual_entries as entries_repo
from app.settings import get_settings, reset_settings_cache
from app.web import dashboard as web_dashboard
from tests.conftest import Actor, requires_db

pytestmark = [requires_db, pytest.mark.db]

SUPABASE_URL = "https://project.supabase.test"
JWT_SECRET = "secreto-compartido-del-supabase-local"


@pytest_asyncio.fixture
async def users(
    app_runtime_engine: None, two_users: tuple[Actor, Actor]
) -> AsyncIterator[tuple[Actor, Actor]]:
    yield two_users


def _claims(actor: Actor) -> dict[str, str]:
    return {"sub": str(actor.id), "role": "authenticated"}


async def _system_category(owner_conn: asyncpg.Connection, kind: str) -> str:
    row = await owner_conn.fetchrow(
        "select id::text from app.categories where user_id is null and kind = $1 and is_active "
        "order by sort_order limit 1",
        kind,
    )
    assert row is not None, f"el seed no trae categorias de kind={kind}"
    return str(row["id"])


async def _private_category(owner_conn: asyncpg.Connection, owner: Actor, kind: str) -> str:
    row = await owner_conn.fetchrow(
        "insert into app.categories (user_id, slug, name, kind) values ($1, $2, 'Privada', $3) "
        "returning id::text",
        owner.id,
        f"priv-{uuid.uuid4().hex[:8]}",
        kind,
    )
    assert row is not None
    return str(row["id"])


async def _count(actor: Actor) -> int:
    async with db.user_tx(_claims(actor)) as conn:
        return len(await entries_repo.list_recent(conn))


@pytest_asyncio.fixture
async def client(
    users: tuple[Actor, Actor], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente autenticado como alice (`users[0]`)."""
    from app.security import session as session_store

    alice = users[0]
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("SESSION_SECRET", "un-secreto-de-mas-de-treinta-y-dos-caracteres")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key-de-prueba")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
    reset_settings_cache()
    settings = get_settings()

    now = int(time.time())
    token = pyjwt.encode(
        {
            "sub": str(alice.id),
            "aud": "authenticated",
            "role": "authenticated",
            "iss": f"{SUPABASE_URL}/auth/v1",
            "email": alice.email,
            "session_id": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 3600,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    cookie = session_store.encode(
        session_store.Session(
            user_id=str(alice.id),
            email=alice.email,
            access_token=token,
            refresh_token="refresh",
            access_expires_at=now + 3600,
        ),
        settings,
    )

    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.session_cookie_name: cookie},
    ) as http_client:
        yield http_client

    reset_settings_cache()


async def _post(client: httpx.AsyncClient, alice: Actor, **overrides: str) -> httpx.Response:
    from app.security import csrf

    form = {
        "entry_date": "2026-03-10",
        "kind": "expense",
        "description": "Alquiler",
        "amount": "1000.00",
        "currency": "ARS",
        "category_id": "",
        "csrf_token": csrf.issue(str(alice.id), get_settings()),
    }
    form.update(overrides)
    return await client.post("/movements", data=form)


def _is_friendly_error(response: httpx.Response) -> bool:
    return response.status_code == 303 and "error=" in response.headers["location"]


class TestCrear:
    async def test_caso_feliz_normaliza_moneda(
        self, client: httpx.AsyncClient, users: tuple[Actor, Actor]
    ) -> None:
        response = await _post(client, users[0], currency=" usd ")
        assert response.status_code == 303
        assert "error=" not in response.headers["location"]
        async with db.user_tx(_claims(users[0])) as conn:
            assert (await entries_repo.list_recent(conn))[0].currency == "USD"

    async def test_categoria_privada_de_otro_usuario_se_rechaza(
        self,
        client: httpx.AsyncClient,
        users: tuple[Actor, Actor],
        owner_conn: asyncpg.Connection,
    ) -> None:
        bobs = await _private_category(owner_conn, users[1], "expense")
        response = await _post(client, users[0], category_id=bobs)
        assert _is_friendly_error(response)
        assert await _count(users[0]) == 0

    async def test_categoria_propia_se_acepta(
        self,
        client: httpx.AsyncClient,
        users: tuple[Actor, Actor],
        owner_conn: asyncpg.Connection,
    ) -> None:
        mine = await _private_category(owner_conn, users[0], "expense")
        response = await _post(client, users[0], category_id=mine)
        assert not _is_friendly_error(response)
        assert await _count(users[0]) == 1

    async def test_kind_de_categoria_no_coincide(
        self,
        client: httpx.AsyncClient,
        users: tuple[Actor, Actor],
        owner_conn: asyncpg.Connection,
    ) -> None:
        income_cat = await _system_category(owner_conn, "income")
        assert _is_friendly_error(await _post(client, users[0], category_id=income_cat))
        expense_cat = await _system_category(owner_conn, "expense")
        assert _is_friendly_error(
            await _post(client, users[0], kind="income", category_id=expense_cat)
        )
        assert await _count(users[0]) == 0

    async def test_uuid_malformado_o_inexistente(
        self, client: httpx.AsyncClient, users: tuple[Actor, Actor]
    ) -> None:
        for bad in ("no-es-uuid", str(uuid.uuid4())):
            assert _is_friendly_error(await _post(client, users[0], category_id=bad))
        assert await _count(users[0]) == 0

    @pytest.mark.parametrize("currency", ["PESOS", "A1B", "AR", "$$$"])
    async def test_moneda_invalida(
        self, client: httpx.AsyncClient, users: tuple[Actor, Actor], currency: str
    ) -> None:
        assert _is_friendly_error(await _post(client, users[0], currency=currency))
        assert await _count(users[0]) == 0

    @pytest.mark.parametrize("amount", ["0", "-5", "1000000000000", "99999999999999", "9" * 40])
    async def test_monto_invalido_o_fuera_de_rango(
        self, client: httpx.AsyncClient, users: tuple[Actor, Actor], amount: str
    ) -> None:
        assert _is_friendly_error(await _post(client, users[0], amount=amount))
        assert await _count(users[0]) == 0

    @pytest.mark.parametrize(
        "field,value",
        [
            ("description", "a\x00b"),
            ("description", "x" * 201),
            ("notes", "a\x00b"),
            ("notes", "x" * 1001),
        ],
    )
    async def test_texto_invalido_o_demasiado_largo(
        self, client: httpx.AsyncClient, users: tuple[Actor, Actor], field: str, value: str
    ) -> None:
        assert _is_friendly_error(await _post(client, users[0], **{field: value}))
        assert await _count(users[0]) == 0

    async def test_monto_maximo_valido(
        self, client: httpx.AsyncClient, users: tuple[Actor, Actor]
    ) -> None:
        response = await _post(client, users[0], amount="999999999999.99")
        assert not _is_friendly_error(response)
        assert await _count(users[0]) == 1


class TestBorrar:
    async def test_id_malformado_da_404(
        self, client: httpx.AsyncClient, users: tuple[Actor, Actor]
    ) -> None:
        from app.security import csrf

        response = await client.post(
            "/movements/no-es-uuid/delete",
            data={"csrf_token": csrf.issue(str(users[0].id), get_settings())},
        )
        assert response.status_code == 404


class TestDashboard:
    async def test_ingreso_manual_se_muestra_positivo(self, users: tuple[Actor, Actor]) -> None:
        alice = users[0]
        async with db.user_tx(_claims(alice)) as conn:
            await entries_repo.create(
                conn,
                entry_date=date(2026, 6, 1),
                kind="income",
                description="Sueldo",
                amount=Decimal("800000.00"),
                currency="ARS",
                category_id=None,
                notes=None,
            )
            snapshot = await web_dashboard._snapshot(conn, analytics.Period(2026, 6))
        assert snapshot["income"]["ARS"] == Decimal("800000.00")

    async def test_manual_sin_categoria_no_cuenta_como_sin_categorizar(
        self, users: tuple[Actor, Actor]
    ) -> None:
        alice = users[0]
        async with db.user_tx(_claims(alice)) as conn:
            await entries_repo.create(
                conn,
                entry_date=date(2026, 7, 1),
                kind="expense",
                description="Efectivo",
                amount=Decimal("100.00"),
                currency="ARS",
                category_id=None,
                notes=None,
            )
            assert await analytics.uncategorized_count(conn, analytics.Period(2026, 7)) == 0
