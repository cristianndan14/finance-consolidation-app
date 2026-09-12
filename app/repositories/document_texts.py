"""Lectura y escritura de `app.document_texts`, la salida de la etapa 1.

El `unique (user_id, document_id, extractor, extractor_version)` con
`on conflict do update` hace que reprocesar un documento con la misma version del
extractor pise el texto anterior en lugar de acumular filas, y que una version
nueva conviva con la vieja en vez de destruirla.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_UPSERT = text(
    """
    insert into app.document_texts (
        document_id, extractor, extractor_version,
        page_texts, full_text, char_count, has_text_layer
    )
    values (
        :document_id, :extractor, :extractor_version,
        cast(:page_texts as jsonb), :full_text, :char_count, :has_text_layer
    )
    on conflict (user_id, document_id, extractor, extractor_version) do update
       set page_texts     = excluded.page_texts,
           full_text      = excluded.full_text,
           char_count     = excluded.char_count,
           has_text_layer = excluded.has_text_layer,
           extracted_at   = now()
    returning id::text as id
    """
)

_LATEST = text(
    """
    select id::text as id, document_id::text as document_id, extractor, extractor_version,
           page_texts, full_text, char_count, has_text_layer, extracted_at
      from app.document_texts
     where document_id = :document_id
     order by extracted_at desc
     limit 1
    """
)


@dataclass(frozen=True)
class DocumentText:
    id: str
    document_id: str
    extractor: str
    extractor_version: str
    page_texts: list[dict[str, Any]]
    full_text: str
    char_count: int
    has_text_layer: bool
    extracted_at: datetime

    @property
    def page_count(self) -> int:
        return len(self.page_texts)


async def upsert(
    conn: AsyncConnection,
    *,
    document_id: str,
    extractor: str,
    extractor_version: str,
    page_texts: list[dict[str, Any]],
    full_text: str,
    char_count: int,
    has_text_layer: bool,
) -> str:
    row = (
        (
            await conn.execute(
                _UPSERT,
                {
                    "document_id": document_id,
                    "extractor": extractor,
                    "extractor_version": extractor_version,
                    "page_texts": json.dumps(page_texts, ensure_ascii=False),
                    "full_text": full_text,
                    "char_count": char_count,
                    "has_text_layer": has_text_layer,
                },
            )
        )
        .mappings()
        .one()
    )
    return str(row["id"])


async def latest_for_document(conn: AsyncConnection, document_id: str) -> DocumentText | None:
    """La extraccion mas reciente. La etapa 2 (Fase 3) parte de aca, no del PDF."""
    row = (await conn.execute(_LATEST, {"document_id": document_id})).mappings().one_or_none()
    if row is None:
        return None
    data = dict(row)
    # asyncpg devuelve jsonb como texto cuando no hay codec registrado.
    if isinstance(data["page_texts"], str):
        data["page_texts"] = json.loads(data["page_texts"])
    return DocumentText(**data)
