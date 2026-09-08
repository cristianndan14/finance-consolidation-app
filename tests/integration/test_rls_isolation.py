"""El test que sostiene el proyecto: un usuario no puede ver los datos de otro.

Si este archivo falla, nada mas importa: la aplicacion no puede tener datos
financieros de varias personas hasta que vuelva a verde.

Tres cosas que hacen que este test valga:

1. Pega como `app_runtime`, el MISMO rol que usa el backend en produccion.
   Conectarse como `postgres` saltearia RLS y el test pasaria siempre, midiendo
   nada.

2. Las tablas NO estan hardcodeadas: se descubren de `pg_catalog`. Si alguien
   agrega una tabla al schema `app` y se olvida de las politicas, aparece sola en
   la parametrizacion y el test falla. Una lista escrita a mano se desactualiza en
   el primer sprint apurado.

3. Verifica las cuatro operaciones (select / insert / update / delete), no solo
   lectura. Poder ESCRIBIR en la fila de otro usuario es igual de grave que poder
   leerla, y es mas facil de dejar abierto por accidente.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from tests.conftest import Actor, requires_db

pytestmark = [requires_db, pytest.mark.db]

# Tablas donde `user_id` puede ser NULL porque hay filas del sistema compartidas.
# Se testean con reglas propias en TestSharedTables.
SHARED_TABLES = {"categories", "fx_rates"}

# Tablas con reglas de acceso propias, cubiertas por sus propios tests.
SPECIAL_TABLES = {"profiles", "invitations"}


async def _owned_tables(conn: asyncpg.Connection) -> list[str]:
    """Tablas de `app` con `user_id NOT NULL`: las de dueño simple."""
    rows = await conn.fetch(
        """
        select c.relname as name
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
          join pg_attribute a on a.attrelid = c.oid
         where n.nspname = 'app'
           and c.relkind = 'r'
           and a.attname = 'user_id'
           and a.attnotnull
         order by c.relname
        """
    )
    return [r["name"] for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Invariantes del esquema
# ─────────────────────────────────────────────────────────────────────────────


async def test_el_rol_de_la_app_no_puede_saltear_rls(app_conn: asyncpg.Connection) -> None:
    """Si `app_runtime` tuviera BYPASSRLS o superuser, todo lo demas es decorativo."""
    row = await app_conn.fetchrow(
        "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
    )
    assert row is not None, "el rol de la conexion no existe en pg_roles"
    assert row["rolsuper"] is False, "app_runtime es superuser: RLS no aplica"
    assert row["rolbypassrls"] is False, "app_runtime tiene BYPASSRLS: RLS no aplica"


async def test_todas_las_tablas_tienen_rls_forzado(owner_conn: asyncpg.Connection) -> None:
    """`enable` no alcanza: sin `force`, el owner de la tabla ve todo."""
    rows = await owner_conn.fetch(
        """
        select c.relname as name, c.relrowsecurity as enabled, c.relforcerowsecurity as forced
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'app' and c.relkind = 'r'
         order by c.relname
        """
    )
    assert rows, "no se encontro ninguna tabla en el schema app"

    sin_rls = [r["name"] for r in rows if not r["enabled"]]
    sin_force = [r["name"] for r in rows if r["enabled"] and not r["forced"]]

    assert not sin_rls, f"tablas sin RLS habilitado: {sin_rls}"
    assert not sin_force, f"tablas sin RLS forzado (el owner las ve enteras): {sin_force}"


async def test_todas_las_tablas_tienen_al_menos_una_politica(
    owner_conn: asyncpg.Connection,
) -> None:
    """RLS sin politicas niega todo: seguro, pero es un bug, no un diseño."""
    rows = await owner_conn.fetch(
        """
        select c.relname as name
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'app' and c.relkind = 'r'
           and not exists (select 1 from pg_policy p where p.polrelid = c.oid)
         order by c.relname
        """
    )
    assert not rows, f"tablas con RLS pero sin politicas: {[r['name'] for r in rows]}"


async def test_las_vistas_usan_security_invoker(owner_conn: asyncpg.Connection) -> None:
    """Una vista sin `security_invoker` lee las tablas base como su creador.

    Es decir: expone las filas de TODOS los usuarios a cualquiera que la consulte.
    Es la forma mas facil de anular RLS sin darse cuenta.
    """
    rows = await owner_conn.fetch(
        """
        select c.relname as name, c.reloptions as opts
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'app' and c.relkind = 'v'
         order by c.relname
        """
    )
    assert rows, "no se encontro ninguna vista en el schema app"

    culpables = [
        r["name"]
        for r in rows
        if not any("security_invoker" in opt and "true" in opt.lower() for opt in (r["opts"] or []))
    ]
    assert not culpables, f"vistas sin security_invoker=true: {culpables}"


# ─────────────────────────────────────────────────────────────────────────────
# Sin identidad no se accede a nada
# ─────────────────────────────────────────────────────────────────────────────


async def test_sin_identidad_no_hay_acceso(app_conn: asyncpg.Connection) -> None:
    """El escenario del pool: una conexion reusada sin haber seteado el contexto.

    `app_runtime` no tiene grants propios (los tiene `authenticated`), asi que el
    fallo es un error de permisos duro, no un resultado vacio que podria pasar
    inadvertido. Son dos mecanismos independientes: falta el grant Y ademas RLS.
    """
    for table in ("cards", "documents", "transactions", "statements"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.fetch(f"select * from app.{table} limit 1")  # noqa: S608


async def test_la_identidad_no_sobrevive_a_la_transaccion(
    app_conn: asyncpg.Connection, two_users: tuple[Actor, Actor]
) -> None:
    """`set_config(..., is_local => true)` tiene que descartarse en el COMMIT.

    Si sobreviviera, la proxima request que tomara esta conexion del pool heredaria
    la identidad del usuario anterior y veria sus finanzas. Es la peor falla
    posible del proyecto y seria silenciosa.
    """
    alice, _ = two_users

    async with alice:
        assert await app_conn.fetchval("select current_user") == "authenticated"
        claims = await app_conn.fetchval("select current_setting('request.jwt.claims', true)")
        assert str(alice.id) in claims

    # Fuera de la transaccion: sin rol y sin claims.
    assert await app_conn.fetchval("select current_user") == "app_runtime"
    assert not await app_conn.fetchval("select current_setting('request.jwt.claims', true)")


# ─────────────────────────────────────────────────────────────────────────────
# Aislamiento tabla por tabla
# ─────────────────────────────────────────────────────────────────────────────


async def test_las_tablas_esperadas_estan_cubiertas(owner_conn: asyncpg.Connection) -> None:
    """Red de seguridad de la parametrizacion.

    Si el descubrimiento devolviera una lista vacia por un cambio de catalogo, los
    tests parametrizados de abajo pasarian sin probar nada. Este test lo impide.
    """
    tables = await _owned_tables(owner_conn)
    assert len(tables) >= 12, (
        f"se esperaban al menos 12 tablas con dueño, hay {len(tables)}: {tables}"
    )
    for expected in ("transactions", "documents", "statements", "cards", "document_texts"):
        assert expected in tables


async def test_bob_no_ve_ninguna_fila_de_alice(
    owner_conn: asyncpg.Connection, two_users: tuple[Actor, Actor]
) -> None:
    """Para CADA tabla con dueño: lo que escribe Alice es invisible para Bob.

    Se inserta con el owner (saltea RLS a proposito, para plantar la fila) y se lee
    como Bob a traves de `app_runtime`, que es el camino real.
    """
    alice, bob = two_users
    tables = await _owned_tables(owner_conn)

    # Alice arma una cadena completa de datos reales.
    async with alice:
        card_id = await alice.conn.fetchval(
            "insert into app.cards (issuer_slug, issuer_name, last4) "
            "values ('galicia','Banco Galicia','4321') returning id"
        )
        doc_id = await alice.conn.fetchval(
            "insert into app.documents (card_id, storage_path, original_filename, "
            "byte_size, sha256) values ($1,$2,'resumen.pdf',2048,$3) returning id",
            card_id,
            f"{alice.id}/2026/x.pdf",
            "a" * 64,
        )
        stmt_id = await alice.conn.fetchval(
            "insert into app.statements (document_id, card_id, currency, period_year, "
            "period_month, new_charges) values ($1,$2,'ARS',2026,3,1000.00) returning id",
            doc_id,
            card_id,
        )
        await alice.conn.execute(
            "insert into app.transactions (statement_id, card_id, posted_date, "
            "description_raw, amount, currency, direction, dedupe_key) "
            "values ($1,$2,'2026-03-15','SUPERMERCADO DIA',1000.00,'ARS',1,$3)",
            stmt_id,
            card_id,
            "b" * 64,
        )

    fallas: list[str] = []
    async with bob:
        for table in tables:
            visto = await bob.conn.fetchval(
                f"select count(*) from app.{table} where user_id = $1",  # noqa: S608
                alice.id,
            )
            if visto:
                fallas.append(f"{table}: Bob ve {visto} filas de Alice")

    assert not fallas, "FILTRACION ENTRE USUARIOS:\n  " + "\n  ".join(fallas)


async def test_bob_no_puede_escribir_con_el_user_id_de_alice(
    two_users: tuple[Actor, Actor],
) -> None:
    """Un INSERT con user_id ajeno falla fuerte, no se corrige en silencio.

    Corregirlo calladamente daria un resultado seguro pero enmascararia un bug de
    la aplicacion (un id mal propagado). Mejor que explote en desarrollo.
    """
    _, bob = two_users
    async with bob:
        # El savepoint va DENTRO de pytest.raises para que reciba la excepcion y
        # haga rollback: si no, la transaccion externa queda abortada.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with bob.conn.transaction():
                await bob.conn.execute(
                    "insert into app.cards (user_id, issuer_slug, issuer_name) "
                    "values ($1,'fraude','Robo de identidad')",
                    uuid.uuid4(),
                )


async def test_bob_no_puede_modificar_ni_borrar_filas_de_alice(
    two_users: tuple[Actor, Actor],
) -> None:
    """Escribir sobre datos ajenos es tan grave como leerlos."""
    alice, bob = two_users

    async with alice:
        card_id = await alice.conn.fetchval(
            "insert into app.cards (issuer_slug, issuer_name) "
            "values ('santander','Santander') returning id"
        )

    async with bob:
        # No hay error: simplemente no hay filas que matchear. El UPDATE afecta 0.
        assert (
            await bob.conn.execute(
                "update app.cards set issuer_name = 'hackeado' where id = $1", card_id
            )
            == "UPDATE 0"
        )
        assert await bob.conn.execute("delete from app.cards where id = $1", card_id) == "DELETE 0"

    # Y la fila de Alice sigue intacta.
    async with alice:
        assert (
            await alice.conn.fetchval("select issuer_name from app.cards where id = $1", card_id)
            == "Santander"
        )


async def test_no_se_puede_regalar_una_fila_a_otro_usuario(
    two_users: tuple[Actor, Actor],
) -> None:
    """Cambiar el user_id de una fila propia tampoco es una operacion valida."""
    alice, bob = two_users

    async with alice:
        card_id = await alice.conn.fetchval(
            "insert into app.cards (issuer_slug, issuer_name) values ('bbva','BBVA') returning id"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with alice.conn.transaction():
                await alice.conn.execute(
                    "update app.cards set user_id = $1 where id = $2", bob.id, card_id
                )


# ─────────────────────────────────────────────────────────────────────────────
# Tablas con reglas propias
# ─────────────────────────────────────────────────────────────────────────────


async def test_borrar_una_tarjeta_no_deja_filas_sin_dueño(
    two_users: tuple[Actor, Actor],
) -> None:
    """Regresion: una FK compuesta con `set null` anulaba tambien `user_id`.

    `foreign key (user_id, card_id) references cards(user_id, id) on delete set
    null` anula TODAS las columnas de la clave, no solo card_id. Borrar una
    tarjeta dejaba sus documentos y transacciones con `user_id` nulo: filas
    huerfanas, invisibles para RLS y, por el NOT NULL, un error en produccion.

    La correccion es `on delete set null (card_id)`, que acota la columna.
    """
    alice, _ = two_users

    async with alice:
        card_id = await alice.conn.fetchval(
            "insert into app.cards (issuer_slug, issuer_name) "
            "values ('macro','Banco Macro') returning id"
        )
        doc_id = await alice.conn.fetchval(
            "insert into app.documents (card_id, storage_path, original_filename, "
            "byte_size, sha256) values ($1,$2,'r.pdf',512,$3) returning id",
            card_id,
            f"{alice.id}/2026/macro.pdf",
            "c" * 64,
        )

        await alice.conn.execute("delete from app.cards where id = $1", card_id)

        row = await alice.conn.fetchrow(
            "select user_id, card_id from app.documents where id = $1", doc_id
        )
        assert row is not None, "el documento desaparecio al borrar la tarjeta"
        assert row["card_id"] is None, "card_id deberia quedar en null"
        assert row["user_id"] == alice.id, "user_id NO debe anularse: la fila perderia dueño"


async def test_solo_se_ve_el_perfil_propio(two_users: tuple[Actor, Actor]) -> None:
    alice, bob = two_users

    async with alice:
        ids = [r["id"] for r in await alice.conn.fetch("select id from app.profiles")]
        assert ids == [alice.id]

    async with bob:
        ids = [r["id"] for r in await bob.conn.fetch("select id from app.profiles")]
        assert ids == [bob.id]


async def test_nadie_se_asciende_a_admin_editando_su_perfil(
    two_users: tuple[Actor, Actor],
) -> None:
    """El `with check` de la politica de update bloquea la escalada de privilegios."""
    alice, _ = two_users

    async with alice:
        # Cambiar datos inocuos si se puede.
        await alice.conn.execute(
            "update app.profiles set display_name = 'Alice', base_currency = 'USD' where id = $1",
            alice.id,
        )

        # El error aborta la transaccion en Postgres, asi que el intento va dentro
        # de un savepoint: si no, la verificacion de abajo no podria ejecutarse.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with alice.conn.transaction():
                await alice.conn.execute(
                    "update app.profiles set role = 'admin' where id = $1", alice.id
                )

        assert await alice.conn.fetchval("select role from app.profiles") == "member"


class TestSharedTables:
    """`categories` y `fx_rates` tienen filas del sistema (`user_id IS NULL`)."""

    async def test_las_categorias_del_sistema_las_ven_todos(
        self, two_users: tuple[Actor, Actor]
    ) -> None:
        alice, bob = two_users
        for actor in (alice, bob):
            async with actor:
                n = await actor.conn.fetchval(
                    "select count(*) from app.categories where user_id is null"
                )
                assert n > 0, "las categorias del sistema no llegan al usuario"

    async def test_una_categoria_propia_no_la_ve_el_otro(
        self, two_users: tuple[Actor, Actor]
    ) -> None:
        alice, bob = two_users

        async with alice:
            await alice.conn.execute(
                "insert into app.categories (user_id, slug, name) values ($1,'mia','Mi categoria')",
                alice.id,
            )

        async with bob:
            assert (
                await bob.conn.fetchval("select count(*) from app.categories where slug = 'mia'")
                == 0
            )

    async def test_no_se_pueden_editar_las_categorias_del_sistema(
        self, two_users: tuple[Actor, Actor]
    ) -> None:
        """Son compartidas: si un usuario pudiera renombrarlas, se las cambia a todos."""
        alice, _ = two_users
        async with alice:
            assert (
                await alice.conn.execute(
                    "update app.categories set name = 'Secuestrada' where user_id is null"
                )
                == "UPDATE 0"
            )
            assert (
                await alice.conn.execute("delete from app.categories where user_id is null")
                == "DELETE 0"
            )
