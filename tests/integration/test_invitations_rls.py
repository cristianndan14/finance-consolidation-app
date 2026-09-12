"""Las invitaciones y el perfil, contra un Postgres real.

`test_rls_isolation.py` cubre las tablas con dueño simple. `app.invitations` no
tiene `user_id`: su politica se apoya en `app.profiles.role`, asi que quien puede
verlas y crearlas se decide con un `exists` contra otra tabla. Eso merece su
propio test, y merece correr por los mismos repositorios que usa la aplicacion —
si el `auth.uid()` de `invited_by` se cambiara por un parametro, este test falla.

Todo pasa por `user_tx()` conectado como `app_runtime`, es decir por el mismo
camino que produccion.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.exc import DBAPIError

from app.infra import db
from app.repositories import invitations as invitations_repo
from app.repositories import profiles as profiles_repo
from tests.conftest import Actor, requires_db

pytestmark = [requires_db, pytest.mark.db]

EXPIRES_AT = datetime(2099, 1, 1, tzinfo=UTC)
LATER = datetime(2099, 6, 1, tzinfo=UTC)


def _claims(user_id: uuid.UUID) -> dict[str, str]:
    return {"sub": str(user_id), "role": "authenticated"}


@pytest_asyncio.fixture
async def admin_and_member(
    owner_conn: asyncpg.Connection,
    app_runtime_engine: None,
    two_users: tuple[Actor, Actor],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Dos usuarios; el primero con `role = 'admin'` en su perfil."""
    admin, member = two_users
    await owner_conn.execute("update app.profiles set role = 'admin' where id = $1", admin.id)
    yield admin.id, member.id


class TestInvitaciones:
    async def test_un_admin_invita_y_queda_registrado_como_quien_invito(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        admin_id, _ = admin_and_member
        async with db.user_tx(_claims(admin_id)) as conn:
            created = await invitations_repo.create(conn, "nuevo@test.local", EXPIRES_AT)

        assert created is not None

        async with db.user_tx(_claims(admin_id)) as conn:
            listed = await invitations_repo.list_all(conn)

        mine = [inv for inv in listed if inv.email == "nuevo@test.local"]
        assert len(mine) == 1
        # `invited_by` sale de auth.uid() en el SQL, no de un parametro: no hay
        # forma de registrar una invitacion a nombre de otro.
        assert mine[0].invited_by_email == f"{admin_id}@test.local"

    async def test_un_miembro_no_puede_invitar(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """El `with check` de la politica rechaza el insert de un no-admin."""
        _, member_id = admin_and_member
        with pytest.raises(DBAPIError) as caught:
            async with db.user_tx(_claims(member_id)) as conn:
                await invitations_repo.create(conn, "colado@test.local", EXPIRES_AT)

        # El dialecto de asyncpg en SQLAlchemy reenvuelve el error, asi que se
        # verifica el motivo y no la clase: lo que importa es que lo rechazo la
        # politica de RLS y no cualquier otro error de SQL.
        assert "violates row-level security policy" in str(caught.value)
        assert "invitations" in str(caught.value)

    async def test_un_miembro_no_ve_las_invitaciones(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """La lista de invitaciones son emails de terceros: no es dato publico."""
        admin_id, member_id = admin_and_member
        async with db.user_tx(_claims(admin_id)) as conn:
            await invitations_repo.create(conn, "privado@test.local", EXPIRES_AT)

        async with db.user_tx(_claims(member_id)) as conn:
            assert await invitations_repo.list_all(conn) == []

    async def test_un_miembro_no_puede_revocar(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        admin_id, member_id = admin_and_member
        async with db.user_tx(_claims(admin_id)) as conn:
            created = await invitations_repo.create(conn, "a-revocar@test.local", EXPIRES_AT)
        assert created is not None

        async with db.user_tx(_claims(member_id)) as conn:
            assert await invitations_repo.revoke(conn, created.id) is False

        async with db.user_tx(_claims(admin_id)) as conn:
            assert any(inv.id == created.id for inv in await invitations_repo.list_all(conn))

    async def test_el_admin_revoca_lo_que_invito(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        admin_id, _ = admin_and_member
        async with db.user_tx(_claims(admin_id)) as conn:
            created = await invitations_repo.create(conn, "chau@test.local", EXPIRES_AT)
        assert created is not None

        async with db.user_tx(_claims(admin_id)) as conn:
            assert await invitations_repo.revoke(conn, created.id) is True

        async with db.user_tx(_claims(admin_id)) as conn:
            assert not any(inv.id == created.id for inv in await invitations_repo.list_all(conn))

    async def test_reinvitar_extiende_en_lugar_de_fallar(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """El email es unico: reinvitar tiene que ser idempotente, no un 500."""
        admin_id, _ = admin_and_member

        async with db.user_tx(_claims(admin_id)) as conn:
            first = await invitations_repo.create(conn, "repetido@test.local", EXPIRES_AT)
        async with db.user_tx(_claims(admin_id)) as conn:
            second = await invitations_repo.create(conn, "repetido@test.local", LATER)

        assert first is not None and second is not None
        assert first.id == second.id
        assert second.expires_at > first.expires_at


class TestPerfil:
    async def test_cada_uno_lee_su_propio_perfil(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """El repositorio no filtra por `user_id`: lo hace la politica."""
        admin_id, member_id = admin_and_member

        async with db.user_tx(_claims(admin_id)) as conn:
            as_admin = await profiles_repo.get_current(conn)
        async with db.user_tx(_claims(member_id)) as conn:
            as_member = await profiles_repo.get_current(conn)

        assert as_admin is not None and as_member is not None
        assert as_admin.id == str(admin_id)
        assert as_member.id == str(member_id)
        assert as_admin.is_admin and not as_member.is_admin

    async def test_sin_identidad_no_hay_perfil(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Un `sub` que no existe no devuelve el perfil de nadie."""
        async with db.user_tx(_claims(uuid.uuid4())) as conn:
            assert await profiles_repo.get_current(conn) is None

    async def test_se_pueden_cambiar_las_preferencias(
        self, admin_and_member: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        _, member_id = admin_and_member

        async with db.user_tx(_claims(member_id)) as conn:
            updated = await profiles_repo.update_current(
                conn, display_name="Miembro", base_currency="USD"
            )

        assert updated is not None
        assert updated.display_name == "Miembro"
        assert updated.base_currency == "USD"
        # `role` no se toca desde el repositorio, y la politica tampoco lo dejaria.
        assert updated.role == "member"
