"""Movimientos manuales, contra un Postgres real.

`test_rls_isolation.py` ya cubre el aislamiento generico de `manual_entries`
(es una tabla con dueño simple, descubierta automaticamente). Este archivo
verifica lo especifico del feature: que el repositorio funcione end to end, y
que la union con `v_tx_enriched` lleve el monto con el signo correcto a
`v_monthly_cashflow` y `v_monthly_accrual`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.infra import db
from app.repositories import manual_entries as entries_repo
from tests.conftest import Actor, requires_db

pytestmark = [requires_db, pytest.mark.db]


@pytest_asyncio.fixture
async def users(
    app_runtime_engine: None, two_users: tuple[Actor, Actor]
) -> AsyncIterator[tuple[Actor, Actor]]:
    """Como `two_users`, pero con el engine de `app.infra.db` ya inicializado.

    `db.user_tx()` (lo que usa el repositorio real) lee el engine global del
    modulo; sin `app_runtime_engine` de por medio no habria ninguno.
    """
    yield two_users


class TestRepositorio:
    async def test_crear_listar_y_borrar(self, users: tuple[Actor, Actor]) -> None:
        alice, _ = users

        async with db.user_tx({"sub": str(alice.id), "role": "authenticated"}) as conn:
            entry_id = await entries_repo.create(
                conn,
                entry_date=date(2026, 3, 10),
                kind="expense",
                description="Alquiler",
                amount=Decimal("150000.00"),
                currency="ars",
                category_id=None,
                notes=None,
            )

            entries = await entries_repo.list_recent(conn)
            assert len(entries) == 1
            assert entries[0].id == entry_id
            assert entries[0].description == "Alquiler"
            assert entries[0].currency == "ARS"  # se normaliza a mayusculas
            assert entries[0].is_income is False

            await entries_repo.delete(conn, entry_id)
            assert await entries_repo.list_recent(conn) == []

    async def test_kind_invalido_no_llega_a_la_base(self, users: tuple[Actor, Actor]) -> None:
        alice, _ = users
        async with db.user_tx({"sub": str(alice.id), "role": "authenticated"}) as conn:
            with pytest.raises(ValueError, match="kind"):
                await entries_repo.create(
                    conn,
                    entry_date=date(2026, 3, 10),
                    kind="ahorro",
                    description="x",
                    amount=Decimal("10.00"),
                    currency="ARS",
                    category_id=None,
                    notes=None,
                )

    async def test_bob_no_ve_los_movimientos_de_alice(self, users: tuple[Actor, Actor]) -> None:
        alice, bob = users

        async with db.user_tx({"sub": str(alice.id), "role": "authenticated"}) as conn:
            await entries_repo.create(
                conn,
                entry_date=date(2026, 3, 1),
                kind="income",
                description="Sueldo",
                amount=Decimal("500000.00"),
                currency="ARS",
                category_id=None,
                notes=None,
            )

        async with db.user_tx({"sub": str(bob.id), "role": "authenticated"}) as conn:
            assert await entries_repo.list_recent(conn) == []

    async def test_bob_no_puede_borrar_los_movimientos_de_alice(
        self, users: tuple[Actor, Actor]
    ) -> None:
        alice, bob = users
        alice_claims = {"sub": str(alice.id), "role": "authenticated"}

        async with db.user_tx(alice_claims) as conn:
            entry_id = await entries_repo.create(
                conn,
                entry_date=date(2026, 3, 1),
                kind="expense",
                description="Alquiler",
                amount=Decimal("150000.00"),
                currency="ARS",
                category_id=None,
                notes=None,
            )

        async with db.user_tx({"sub": str(bob.id), "role": "authenticated"}) as conn:
            await entries_repo.delete(conn, entry_id)

        async with db.user_tx(alice_claims) as conn:
            assert [e.id for e in await entries_repo.list_recent(conn)] == [entry_id]


class TestEnLasVistas:
    async def test_gasto_manual_suma_como_consumo_en_cashflow_y_devengado(
        self, users: tuple[Actor, Actor]
    ) -> None:
        alice, _ = users

        async with db.user_tx({"sub": str(alice.id), "role": "authenticated"}) as conn:
            await entries_repo.create(
                conn,
                entry_date=date(2026, 5, 15),
                kind="expense",
                description="Efectivo supermercado",
                amount=Decimal("20000.00"),
                currency="ARS",
                category_id=None,
                notes=None,
            )

            for view in ("app.v_monthly_cashflow", "app.v_monthly_accrual"):
                row = (
                    (
                        await conn.execute(
                            text(
                                f"select category_kind, total from {view} "  # noqa: S608
                                "where period_year = 2026 and period_month = 5"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["category_kind"] == "expense"
                assert Decimal(str(row["total"])) == Decimal("20000.00")

    async def test_ingreso_manual_no_se_mezcla_con_el_consumo(
        self, users: tuple[Actor, Actor]
    ) -> None:
        alice, _ = users

        async with db.user_tx({"sub": str(alice.id), "role": "authenticated"}) as conn:
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

            rows = (
                await conn.execute(
                    text(
                        "select category_kind, total from app.v_monthly_cashflow "
                        "where period_year = 2026 and period_month = 6"
                    )
                )
            ).mappings()
            by_kind = {r["category_kind"]: Decimal(str(r["total"])) for r in rows}

            assert by_kind.get("expense") is None
            assert by_kind["income"] == Decimal("-800000.00")


class TestElKindDelMovimientoManda:
    """El `kind` del movimiento decide `category_kind`, no la categoria elegida.

    Regresion: la rama manual de `v_tx_enriched` usaba
    `coalesce(cat.kind, e.kind)`, asi que un ingreso con una categoria de gasto
    terminaba como un gasto negativo (achicando el consumo) y viceversa. Se
    inserta directo en la tabla, sin pasar por la validacion de la web, porque
    lo que se prueba es la vista.
    """

    @staticmethod
    async def _insert(
        conn: AsyncConnection, *, kind: str, category_slug: str, amount: str, day: date
    ) -> None:
        await conn.execute(
            text(
                "insert into app.manual_entries "
                "(user_id, entry_date, kind, description, amount, currency, category_id) "
                "select cast(auth.uid() as uuid), :d, :k, 'x', cast(:a as numeric), 'ARS', id "
                "from app.categories where slug = :slug and user_id is null"
            ),
            {"d": day, "k": kind, "a": amount, "slug": category_slug},
        )

    @staticmethod
    async def _totals_by_kind(conn: AsyncConnection, view: str, month: int) -> dict[str, Decimal]:
        rows = (
            await conn.execute(
                text(
                    f"select category_kind, sum(total) as total from {view} "  # noqa: S608
                    "where period_year = 2026 and period_month = :m group by category_kind"
                ),
                {"m": month},
            )
        ).mappings()
        return {r["category_kind"]: Decimal(str(r["total"])) for r in rows}

    async def test_ingreso_con_categoria_de_gasto_cuenta_como_ingreso(
        self, users: tuple[Actor, Actor]
    ) -> None:
        alice, _ = users
        async with db.user_tx({"sub": str(alice.id), "role": "authenticated"}) as conn:
            await self._insert(
                conn, kind="income", category_slug="otros", amount="1000.00", day=date(2026, 7, 3)
            )
            for view in (
                "app.v_monthly_cashflow",
                "app.v_monthly_accrual",
                "app.v_monthly_base_accrual",
            ):
                by_kind = await self._totals_by_kind(conn, view, 7)
                assert by_kind == {"income": Decimal("-1000.00")}, view

    async def test_gasto_con_categoria_de_ingreso_cuenta_como_gasto(
        self, users: tuple[Actor, Actor]
    ) -> None:
        alice, _ = users
        async with db.user_tx({"sub": str(alice.id), "role": "authenticated"}) as conn:
            await self._insert(
                conn,
                kind="expense",
                category_slug="devoluciones",
                amount="2500.00",
                day=date(2026, 8, 20),
            )
            for view in (
                "app.v_monthly_cashflow",
                "app.v_monthly_accrual",
                "app.v_monthly_base_accrual",
            ):
                by_kind = await self._totals_by_kind(conn, view, 8)
                assert by_kind == {"expense": Decimal("2500.00")}, view
