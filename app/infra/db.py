"""Acceso a Postgres. Unico dueño del pool de conexiones.

# Por que este modulo existe y por que es el unico que abre conexiones

El requisito central del proyecto es que un usuario no pueda ver las finanzas de
otro. Esa garantia NO puede depender de que cada query recuerde poner
`where user_id = ...`, porque alcanza un olvido para filtrar datos financieros de
un familiar.

La garantia la da Postgres, con tres piezas que tienen que estar todas:

1. El backend se conecta con el rol `app_runtime`, que **no** tiene `BYPASSRLS` y
   **no** es owner de las tablas. La `service_role` key de Supabase si bypassea RLS,
   y por eso no se usa para datos de negocio (ver `supabase_admin.py`).
2. Cada transaccion declara quien es el usuario actual con
   `set_config('request.jwt.claims', ..., true)`, que es de donde `auth.uid()` saca
   el `sub`. Las politicas RLS comparan contra eso.
3. Todas las tablas tienen `force row level security`.

El resultado es que un query sin filtro devuelve **cero filas** en lugar de las filas
de otro usuario: el modo de falla es seguro.

# El detalle que hace o rompe todo: `is_local => true`

El tercer argumento de `set_config` es `is_local`. En `true`, el valor vive hasta el
`COMMIT`/`ROLLBACK`. En `false` (el default), vive en la **conexion**.

Las conexiones se reusan desde un pool. Si la identidad sobrevive al commit, la
proxima request que tome esa conexion hereda la identidad del usuario anterior y ve
sus datos. Es la peor falla posible aca, es silenciosa, y no aparece en desarrollo
con un solo usuario.

Por eso `SET SESSION`, `SET ROLE` y `set_config(..., false)` estan prohibidos en el
repo, y `scripts/check_local_config.py` lo verifica en cada commit.

# Como se usa

    async with user_tx(request.state.claims) as conn:
        rows = await documents_repo.list_for_user(conn)

Ninguna funcion de `app/repositories/` abre su propia conexion: todas reciben una
`conn` ya contextualizada. Eso es lo que hace imposible que un repositorio se saltee
el contexto por accidente.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.logging_config import get_logger
from app.settings import Settings, get_settings

log = get_logger(__name__)

# El rol al que apuntan todas las politicas RLS.
AUTHENTICATED_ROLE: Final = "authenticated"

# Claims que se propagan a Postgres. Lista blanca deliberada.
#
# `user_metadata` de Supabase es escribible por el propio usuario: si una politica
# leyera de ahi, el usuario podria elegir que ve. Nunca se propaga.
PROPAGATED_CLAIMS: Final = frozenset({"sub", "role", "email", "session_id", "aud"})

# Un solo statement para el contexto: un round-trip, no dos.
_SET_CONTEXT: Final = text(
    "select set_config('role', :role, true), set_config('request.jwt.claims', :claims, true)"
)

_engine: AsyncEngine | None = None


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Crea el engine. Se llama una vez, desde el lifespan de FastAPI."""
    global _engine
    if _engine is not None:
        return _engine

    cfg = settings or get_settings()
    _engine = create_async_engine(
        cfg.database_url,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_pre_ping=True,
        # Las conexiones se recielan para que un failover de Supabase no deje
        # conexiones muertas en el pool.
        pool_recycle=1800,
        connect_args={
            "command_timeout": cfg.db_command_timeout_seconds,
            "server_settings": {"application_name": "finance-consolidation"},
        },
        echo=False,  # las queries llevan datos financieros: nunca al log
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("el engine no fue inicializado; llamar init_engine() en el lifespan")
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def _sanitize_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """Deja pasar solo los claims de la lista blanca y exige un `sub` usable."""
    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise ValueError("los claims no tienen un 'sub' valido: no se puede establecer identidad")

    safe = {k: v for k, v in claims.items() if k in PROPAGATED_CLAIMS}
    # El rol lo fija el backend, no el token.
    safe["role"] = AUTHENTICATED_ROLE
    return safe


@asynccontextmanager
async def user_tx(claims: dict[str, Any]) -> AsyncIterator[AsyncConnection]:
    """Transaccion con la identidad del usuario propagada a Postgres.

    Todo lo que lea o escriba datos de negocio pasa por aca. Al salir del bloque,
    el `COMMIT` descarta el contexto (`is_local => true`), asi que la conexion
    vuelve al pool sin identidad.
    """
    safe_claims = _sanitize_claims(claims)

    async with get_engine().begin() as conn:
        await conn.execute(
            _SET_CONTEXT,
            {"role": AUTHENTICATED_ROLE, "claims": json.dumps(safe_claims, separators=(",", ":"))},
        )
        yield conn


@asynccontextmanager
async def system_tx(user_id: str) -> AsyncIterator[AsyncConnection]:
    """Transaccion para jobs de background, que no tienen JWT del usuario.

    Un worker podria conectarse con `service_role` y hacer lo que quiera. No lo
    hace: sintetiza los claims minimos del dueño del job y usa el mismo rol
    `app_runtime`, de modo que **RLS sigue activo**. Un bug en un handler de job no
    puede escribir en los datos de otro usuario.

    Cuesta cero y elimina una clase entera de bugs.
    """
    async with user_tx({"sub": user_id, "role": AUTHENTICATED_ROLE}) as conn:
        yield conn


@asynccontextmanager
async def runtime_tx() -> AsyncIterator[AsyncConnection]:
    """Transaccion SIN identidad de usuario, como `app_runtime` pelado.

    Existe para exactamente dos operaciones, las unicas del sistema que cruzan
    usuarios: tomar el proximo job de la cola y reciclar los huerfanos. Las dos
    son funciones `security definer` cuyo `execute` esta otorgado solo a
    `app_runtime` (ver la migracion `job_dispatch`).

    **No sirve para leer datos de negocio**: sin `request.jwt.claims`, `auth.uid()`
    es NULL y las politicas no matchean nada, asi que un select devuelve cero
    filas. Ese es justamente el comportamiento deseado — si alguien la usa por
    error para leer documentos, no ve los de otro: no ve ninguno.
    """
    async with get_engine().begin() as conn:
        yield conn


async def check_connection() -> dict[str, Any]:
    """Diagnostico para /healthz/db.

    Verifica lo que importa: que se este conectado con el rol correcto y que ese
    rol no pueda saltearse RLS. Si `rolbypassrls` fuera `true`, el aislamiento
    entre usuarios seria decorativo y el endpoint tiene que gritarlo.
    """
    async with get_engine().connect() as conn:
        row = (
            await conn.execute(
                text(
                    "select current_user, "
                    "       (select rolbypassrls from pg_roles where rolname = current_user) "
                    "         as bypasses_rls, "
                    "       (select rolsuper from pg_roles where rolname = current_user) "
                    "         as is_superuser, "
                    "       version() as version"
                )
            )
        ).one()

    unsafe = bool(row.bypasses_rls) or bool(row.is_superuser)
    if unsafe:
        log.error(
            "el rol de la aplicacion puede saltear RLS: el aislamiento entre usuarios no aplica",
            role=row.current_user,
            bypasses_rls=bool(row.bypasses_rls),
            is_superuser=bool(row.is_superuser),
        )

    return {
        "connected": True,
        "role": row.current_user,
        "rls_enforced": not unsafe,
        "server_version": row.version.split()[1] if row.version else None,
    }
