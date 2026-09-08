"""Aplicador de migraciones SQL.

Existe en vez de usar solo el CLI de Supabase porque los tests de integracion (y
CI) corren contra un Postgres pelado, sin Docker de Supabase. Y eso importa: si
los tests validaran un esquema armado de otra forma, el test de aislamiento RLS
estaria certificando algo que no es lo que se despliega, lo cual es peor que no
tenerlo.

Es el mismo directorio de migraciones, en el mismo orden, en los dos entornos.

Se conecta con el rol OWNER (postgres), no con `app_runtime`: crear tablas y
politicas necesita privilegios que el rol de la aplicacion no tiene ni debe tener.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "supabase" / "migrations"
SEED_FILE = MIGRATIONS_DIR.parent / "seed.sql"

# Tabla de control propia, en su propio schema, para no chocar con la de Supabase.
_LEDGER = """
create schema if not exists _fc;
create table if not exists _fc.applied_migrations (
    filename   text primary key,
    applied_at timestamptz not null default now()
);
"""


@dataclass(frozen=True)
class Migration:
    filename: str
    sql: str


def discover() -> list[Migration]:
    """Migraciones ordenadas por nombre (el prefijo timestamp define el orden)."""
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"no existe el directorio de migraciones: {MIGRATIONS_DIR}")

    files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
    return [Migration(p.name, p.read_text(encoding="utf-8")) for p in files]


def _to_asyncpg_dsn(url: str) -> str:
    """Convierte un DSN de SQLAlchemy a uno que entienda asyncpg."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


async def apply_all(
    url: str,
    *,
    seed: bool = True,
    app_runtime_password: str | None = None,
    force: bool = False,
) -> list[str]:
    """Aplica las migraciones pendientes. Devuelve las que corrio.

    Cada migracion va en su propia transaccion: si una falla, las anteriores
    quedan aplicadas y registradas, y se puede corregir y reintentar sin repetir.
    """
    conn = await asyncpg.connect(_to_asyncpg_dsn(url))
    applied: list[str] = []
    try:
        await conn.execute(_LEDGER)

        done: set[str] = (
            set()
            if force
            else {
                r["filename"]
                for r in await conn.fetch("select filename from _fc.applied_migrations")
            }
        )

        for migration in discover():
            if migration.filename in done:
                continue
            async with conn.transaction():
                await conn.execute(migration.sql)
                await conn.execute(
                    "insert into _fc.applied_migrations (filename) values ($1) "
                    "on conflict (filename) do nothing",
                    migration.filename,
                )
            applied.append(migration.filename)

        # La contraseña del rol de la aplicacion no puede vivir en una migracion
        # versionada, asi que se aplica aca desde el entorno.
        if app_runtime_password:
            await conn.execute(
                f"alter role app_runtime password {_quote_literal(app_runtime_password)}"
            )

        if seed and SEED_FILE.is_file():
            async with conn.transaction():
                await conn.execute(SEED_FILE.read_text(encoding="utf-8"))

        return applied
    finally:
        await conn.close()


def _quote_literal(value: str) -> str:
    """Literal SQL escapado.

    `alter role ... password` no acepta parametros bindeados, asi que el valor se
    interpola. Se escapan las comillas simples y se rechaza el byte nulo, que
    trunca strings en el protocolo.
    """
    if "\x00" in value:
        raise ValueError("la contraseña no puede contener un byte nulo")
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


async def drop_all(url: str) -> None:
    """Borra todo lo que crean las migraciones. Solo para tests."""
    conn = await asyncpg.connect(_to_asyncpg_dsn(url))
    try:
        await conn.execute(
            """
            drop schema if exists app cascade;
            drop schema if exists _fc cascade;
            drop table if exists auth.users cascade;
            drop function if exists auth.uid() cascade;
            """
        )
    finally:
        await conn.close()
