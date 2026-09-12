"""Fixtures compartidas.

Los tests de integracion necesitan un Postgres. Se toma de `TEST_DATABASE_URL`
(owner) y, si no esta, se saltean con un mensaje que dice como levantarlo. No se
inventa una base ni se mockea: un test de aislamiento contra un mock no prueba
nada, porque lo que se esta verificando es el comportamiento de Postgres.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

OWNER_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("MIGRATION_DATABASE_URL")
APP_PASSWORD = os.environ.get("TEST_APP_RUNTIME_PASSWORD", "app_runtime_test")

SKIP_REASON = (
    "sin base de datos de prueba. Levantar una con:\n"
    "  docker run -d --name fc_test_pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:15\n"
    "y exportar:\n"
    "  TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres"
)

requires_db = pytest.mark.skipif(not OWNER_URL, reason=SKIP_REASON)


def _plain_dsn(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _app_runtime_dsn(owner_url: str) -> str:
    """Mismo host y base que el owner, pero con las credenciales de app_runtime.

    Es deliberado que los tests peguen como `app_runtime` y no como `postgres`:
    conectarse como owner (superuser) saltearia RLS y los tests pasarian siempre.
    """
    dsn = _plain_dsn(owner_url)
    return re.sub(r"^postgresql://[^@]+@", f"postgresql://app_runtime:{APP_PASSWORD}@", dsn)


@pytest_asyncio.fixture(scope="session")
async def migrated_db() -> AsyncIterator[str]:
    """Aplica las migraciones una vez por sesion y devuelve el DSN del owner."""
    if not OWNER_URL:
        pytest.skip(SKIP_REASON)

    from app.cli import migrations

    await migrations.drop_all(OWNER_URL)
    await migrations.apply_all(OWNER_URL, seed=True, app_runtime_password=APP_PASSWORD, force=True)
    yield OWNER_URL


@pytest_asyncio.fixture
async def app_runtime_engine(migrated_db: str) -> AsyncIterator[None]:
    """Inicializa el engine de `app.infra.db` apuntando a la base de prueba.

    Los tests que ejercitan repositorios necesitan pasar por `user_tx()` — que es
    el unico lugar que setea el contexto de RLS — y `user_tx()` usa el engine
    global del modulo. Se conecta como `app_runtime`, igual que produccion: como
    owner, RLS no aplicaria y el test no probaria nada.
    """
    from app.infra import db
    from app.settings import Settings

    dsn = _app_runtime_dsn(migrated_db).replace("postgresql://", "postgresql+asyncpg://", 1)
    await db.dispose_engine()
    db.init_engine(Settings(_env_file=None, database_url=dsn))
    try:
        yield
    finally:
        await db.dispose_engine()


@pytest_asyncio.fixture
async def owner_conn(migrated_db: str) -> AsyncIterator[asyncpg.Connection]:
    """Conexion como owner. Solo para armar datos previos y para introspeccion."""
    conn = await asyncpg.connect(_plain_dsn(migrated_db))
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(migrated_db: str) -> AsyncIterator[asyncpg.Connection]:
    """Conexion como `app_runtime`: el mismo rol que usa el backend en produccion."""
    conn = await asyncpg.connect(_app_runtime_dsn(migrated_db))
    try:
        yield conn
    finally:
        await conn.close()


class Actor:
    """Un usuario de prueba, con su id y una forma de actuar como el."""

    def __init__(self, conn: asyncpg.Connection, user_id: uuid.UUID, email: str) -> None:
        self.conn = conn
        self.id = user_id
        self.email = email

    async def __aenter__(self) -> Actor:
        self._tx = self.conn.transaction()
        await self._tx.start()
        # Exactamente lo que hace app/infra/db.user_tx().
        await self.conn.execute(
            "select set_config('role','authenticated',true), "
            "       set_config('request.jwt.claims',$1,true)",
            f'{{"sub":"{self.id}","role":"authenticated"}}',
        )
        return self

    async def __aexit__(self, exc_type: object, *_rest: object) -> None:
        # Se commitea para que lo que escribe un actor sea visible al siguiente:
        # el test de aislamiento necesita datos realmente persistidos, no de una
        # transaccion abierta. La limpieza la hace el cascade de auth.users.
        if exc_type is None:
            await self._tx.commit()
        else:
            await self._tx.rollback()


@pytest_asyncio.fixture
async def two_users(
    owner_conn: asyncpg.Connection, app_conn: asyncpg.Connection
) -> AsyncIterator[tuple[Actor, Actor]]:
    """Dos usuarios distintos, con perfil creado por el trigger de auth.users."""
    ids = [uuid.uuid4(), uuid.uuid4()]
    emails = [f"{i}@test.local" for i in ids]

    for user_id, email in zip(ids, emails, strict=True):
        await owner_conn.execute(
            "insert into auth.users (id, email) values ($1, $2) on conflict do nothing",
            user_id,
            email,
        )

    yield (
        Actor(app_conn, ids[0], emails[0]),
        Actor(app_conn, ids[1], emails[1]),
    )

    for user_id in ids:
        await owner_conn.execute("delete from auth.users where id = $1", user_id)
