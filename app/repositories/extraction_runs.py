"""Lectura y escritura de `app.extraction_runs`: la bitacora de la etapa 2.

Una fila por cada vez que se le pidio algo al modelo sobre un documento. Se abre
antes de llamar y se cierra despues, de modo que un job que muere a mitad deja la
fila en `running` — y eso es informacion, no basura: dice que se llamo y no se
sabe con que resultado.

`raw_response` se guarda entero a proposito. Permite re-derivar transacciones con
logica de parseo nueva **sin volver a pagarle al modelo**, que es lo que hace
barato iterar el parseo sobre el corpus real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

STAGE_PARSE: Final = "parse"
STAGE_REPAIR: Final = "repair"
STAGE_ENRICH: Final = "enrich"

_START = text(
    """
    insert into app.extraction_runs (
        document_id, document_text_id, stage, provider, model, prompt_version
    )
    values (
        :document_id, :document_text_id, :stage, :provider, :model, :prompt_version
    )
    returning id::text as id
    """
)

_FINISH = text(
    """
    update app.extraction_runs
       set status        = :status,
           input_tokens  = :input_tokens,
           output_tokens = :output_tokens,
           cost_usd      = :cost_usd,
           latency_ms    = :latency_ms,
           raw_response  = cast(:raw_response as jsonb),
           validation    = cast(:validation as jsonb),
           error         = :error,
           finished_at   = now()
     where id = :id
    """
)

# Las corridas anteriores del mismo documento quedan como `superseded`, no se
# borran: son la unica forma de comparar que hacia el prompt viejo contra el
# nuevo sobre el mismo PDF.
_SUPERSEDE = text(
    """
    update app.extraction_runs
       set status = 'superseded'
     where document_id = :document_id
       and stage = :stage
       and id <> :current_id
       and status in ('running', 'succeeded')
    """
)

_LATEST_SUCCEEDED = text(
    """
    select validation
      from app.extraction_runs
     where document_id = :document_id and stage = 'parse' and status = 'succeeded'
     order by started_at desc
     limit 1
    """
)

_LATEST = text(
    """
    select id::text as id, document_id::text as document_id, stage, provider, model,
           prompt_version, status, input_tokens, output_tokens, cost_usd, latency_ms,
           validation, error, started_at, finished_at
      from app.extraction_runs
     where document_id = :document_id
     order by started_at desc
     limit :limit
    """
)


@dataclass(frozen=True)
class ExtractionRun:
    id: str
    document_id: str
    stage: str
    provider: str
    model: str
    prompt_version: str
    status: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    latency_ms: int | None
    validation: dict[str, Any] | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None


async def start(
    conn: AsyncConnection,
    *,
    document_id: str,
    document_text_id: str | None,
    stage: str,
    provider: str,
    model: str,
    prompt_version: str,
) -> str:
    row = (
        (
            await conn.execute(
                _START,
                {
                    "document_id": document_id,
                    "document_text_id": document_text_id,
                    "stage": stage,
                    "provider": provider,
                    "model": model,
                    "prompt_version": prompt_version,
                },
            )
        )
        .mappings()
        .one()
    )
    return str(row["id"])


async def finish(
    conn: AsyncConnection,
    run_id: str,
    *,
    status: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal = Decimal("0"),
    latency_ms: int = 0,
    raw_response: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    await conn.execute(
        _FINISH,
        {
            "id": run_id,
            "status": status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "raw_response": json.dumps(raw_response, ensure_ascii=False, default=str)
            if raw_response is not None
            else None,
            "validation": json.dumps(validation, ensure_ascii=False, default=str)
            if validation is not None
            else None,
            "error": error[:2000] if error else None,
        },
    )


async def supersede_previous(
    conn: AsyncConnection, *, document_id: str, stage: str, current_id: str
) -> None:
    await conn.execute(
        _SUPERSEDE, {"document_id": document_id, "stage": stage, "current_id": current_id}
    )


async def latest_for_document(
    conn: AsyncConnection, document_id: str, *, limit: int = 10
) -> list[ExtractionRun]:
    rows = (await conn.execute(_LATEST, {"document_id": document_id, "limit": limit})).mappings()
    return [ExtractionRun(**_decode(dict(row))) for row in rows]


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    """asyncpg devuelve jsonb como texto cuando no hay codec registrado."""
    if isinstance(row.get("validation"), str):
        row["validation"] = json.loads(row["validation"])
    return row


async def latest_validation(conn: AsyncConnection, document_id: str) -> dict[str, Any]:
    """El reporte de la ultima corrida exitosa. `{}` si no hubo ninguna.

    La pantalla de revision lo usa para poder explicar las filas que sobrevivieron
    a una re-corrida: sin esto, una transaccion que el modelo dejo de ver pero que
    vos habias confirmado aparece en la tabla sin ninguna señal de por que sigue
    ahi, y se lee como un duplicado.
    """
    row = (
        (await conn.execute(_LATEST_SUCCEEDED, {"document_id": document_id}))
        .mappings()
        .one_or_none()
    )
    if row is None or row["validation"] is None:
        return {}

    validation = row["validation"]
    if isinstance(validation, str):
        validation = json.loads(validation)
    return dict(validation)
