"""Lectura y escritura de `app.documents`.

Como el resto de los repositorios, recibe la `conn` de `user_tx()` y no filtra
por `user_id`: lo hace la politica. El `user_id` de cada fila lo completa el
trigger `app.force_owner`, no el INSERT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_SELECT = """
select
    id::text          as id,
    card_id::text     as card_id,
    storage_bucket,
    storage_path,
    original_filename,
    byte_size,
    sha256,
    page_count,
    was_encrypted,
    status,
    failure_reason,
    uploaded_at,
    updated_at
  from app.documents
"""

# Las consultas se escriben completas en lugar de componerse con f-strings: el
# lint de bandit no puede distinguir una interpolacion constante de una con datos
# del usuario, y tener SQL armado por concatenacion en el repo invita a que la
# proxima si lleve datos.
_BY_SHA = text(_SELECT + " where sha256 = :sha256")
_BY_ID = text(_SELECT + " where id = :id")
_LIST = text(_SELECT + " order by uploaded_at desc limit :limit")

_INSERT = text(
    """
    insert into app.documents (
        card_id, storage_path, original_filename, byte_size, sha256,
        page_count, was_encrypted
    )
    values (
        :card_id, :storage_path, :original_filename, :byte_size, :sha256,
        :page_count, :was_encrypted
    )
    returning
    id::text          as id,
    card_id::text     as card_id,
    storage_bucket,
    storage_path,
    original_filename,
    byte_size,
    sha256,
    page_count,
    was_encrypted,
    status,
    failure_reason,
    uploaded_at,
    updated_at
    """
)

_SET_STATUS = text(
    """
    update app.documents
       set status = :status,
           failure_reason = :failure_reason
     where id = :id
    """
)


@dataclass(frozen=True)
class Document:
    id: str
    card_id: str | None
    storage_bucket: str
    storage_path: str
    original_filename: str
    byte_size: int
    sha256: str
    page_count: int | None
    was_encrypted: bool
    status: str
    failure_reason: str | None
    uploaded_at: datetime
    updated_at: datetime

    @property
    def is_processing(self) -> bool:
        """Si la UI todavia tiene que seguir preguntando por el estado."""
        return self.status == "uploaded"

    @property
    def failed(self) -> bool:
        return self.status == "failed"


def _row(row: Any) -> Document:
    return Document(**row)


async def get(conn: AsyncConnection, document_id: str) -> Document | None:
    row = (await conn.execute(_BY_ID, {"id": document_id})).mappings().one_or_none()
    return _row(row) if row else None


async def get_by_sha256(conn: AsyncConnection, sha256: str) -> Document | None:
    """Para la idempotencia del upload: el mismo archivo no se importa dos veces."""
    row = (await conn.execute(_BY_SHA, {"sha256": sha256})).mappings().one_or_none()
    return _row(row) if row else None


async def list_recent(conn: AsyncConnection, limit: int = 100) -> list[Document]:
    rows = (await conn.execute(_LIST, {"limit": limit})).mappings().all()
    return [_row(row) for row in rows]


async def create(
    conn: AsyncConnection,
    *,
    storage_path: str,
    original_filename: str,
    byte_size: int,
    sha256: str,
    page_count: int,
    was_encrypted: bool,
    card_id: str | None = None,
) -> Document:
    row = (
        (
            await conn.execute(
                _INSERT,
                {
                    "card_id": card_id,
                    "storage_path": storage_path,
                    "original_filename": original_filename,
                    "byte_size": byte_size,
                    "sha256": sha256,
                    "page_count": page_count,
                    "was_encrypted": was_encrypted,
                },
            )
        )
        .mappings()
        .one()
    )
    return _row(row)


async def set_status(
    conn: AsyncConnection,
    document_id: str,
    status: str,
    *,
    failure_reason: str | None = None,
) -> None:
    await conn.execute(
        _SET_STATUS,
        {"id": document_id, "status": status, "failure_reason": failure_reason},
    )
