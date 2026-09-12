"""Cola de trabajos: `app.processing_jobs`.

Dos caminos distintos y deliberadamente separados:

- **Encolar y consultar** pasan por `user_tx()`: son operaciones del usuario
  sobre sus propios jobs, con RLS como cualquier otra tabla.
- **Tomar el proximo job** y **reciclar los huerfanos** cruzan usuarios por
  definicion, y van por las dos funciones `security definer` de la migracion
  `job_dispatch`. Es la unica parte del sistema que ve filas de varios dueños, y
  esta acotada a dos funciones que no hacen nada mas.

El `unique index jobs_active_uq` hace que encolar dos veces el mismo trabajo para
el mismo documento sea un no-op en lugar de una corrida duplicada.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Los tipos de job del sistema. El check de la tabla tiene la misma lista.
EXTRACT_TEXT: Final = "extract_text"
PARSE_STATEMENT: Final = "parse_statement"
ENRICH: Final = "enrich"

_ENQUEUE = text(
    """
    insert into app.processing_jobs (document_id, job_type, payload, max_attempts)
    values (:document_id, :job_type, cast(:payload as jsonb), :max_attempts)
    on conflict do nothing
    returning id::text as id
    """
)

_CLAIM = text(
    """
    select id::text as id, user_id::text as user_id, document_id::text as document_id,
           job_type, payload, attempts, max_attempts
      from app.claim_next_job(:worker)
    """
)

_REAP = text("select app.reap_stale_jobs(make_interval(mins => :minutes)) as reaped")

_SUCCEED = text(
    """
    update app.processing_jobs
       set status = 'succeeded', finished_at = now(), error = null,
           result = cast(:result as jsonb), locked_at = null, locked_by = null
     where id = :id
    """
)

_FAIL = text(
    """
    update app.processing_jobs
       set status = case when attempts >= max_attempts then 'failed' else 'queued' end,
           error = :error,
           finished_at = case when attempts >= max_attempts then now() else null end,
           -- Backoff exponencial: 30s, 60s, 120s. Un fallo transitorio (Storage
           -- caido) se recupera solo; uno permanente no ocupa un worker.
           scheduled_at = now() + make_interval(secs => 30 * power(2, attempts)::int),
           locked_at = null, locked_by = null
     where id = :id
    returning status
    """
)

# Un fallo permanente no vuelve a la cola: reintentar un PDF que no tiene texto
# da el mismo resultado tres veces y solo ocupa un worker.
_FAIL_PERMANENT = text(
    """
    update app.processing_jobs
       set status = 'failed', error = :error, finished_at = now(),
           locked_at = null, locked_by = null
     where id = :id
    """
)

_LATEST_FOR_DOCUMENT = text(
    """
    select id::text as id, job_type, status, attempts, max_attempts, error,
           created_at, finished_at
      from app.processing_jobs
     where document_id = :document_id
     order by created_at desc
     limit 1
    """
)


@dataclass(frozen=True)
class ClaimedJob:
    """Un job tomado por el runner. `user_id` es lo que le da identidad al handler."""

    id: str
    user_id: str
    document_id: str | None
    job_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


@dataclass(frozen=True)
class JobStatus:
    """Lo que la UI necesita para decir "procesando" o "fallo"."""

    id: str
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.status in ("queued", "running")


async def enqueue(
    conn: AsyncConnection,
    *,
    document_id: str,
    job_type: str,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> str | None:
    """Encola un job. Devuelve `None` si ya habia uno activo igual.

    El `on conflict do nothing` se apoya en `jobs_active_uq`: dos clicks en
    "reprocesar" no producen dos corridas.
    """
    row = (
        (
            await conn.execute(
                _ENQUEUE,
                {
                    "document_id": document_id,
                    "job_type": job_type,
                    "payload": json.dumps(payload or {}),
                    "max_attempts": max_attempts,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return str(row["id"]) if row else None


async def claim_next(conn: AsyncConnection, worker: str) -> ClaimedJob | None:
    """Toma el proximo job de la cola, de cualquier usuario.

    La conexion **no** debe tener el contexto de un usuario: la funcion es
    `security definer` y solo `app_runtime` puede ejecutarla.
    """
    row = (await conn.execute(_CLAIM, {"worker": worker})).mappings().one_or_none()
    if row is None:
        return None

    payload = row["payload"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    return ClaimedJob(
        id=row["id"],
        user_id=row["user_id"],
        document_id=row["document_id"],
        job_type=row["job_type"],
        payload=dict(payload),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
    )


async def reap_stale(conn: AsyncConnection, stale_minutes: int) -> int:
    """Devuelve a la cola los jobs cuyo proceso murio. Retorna cuantos."""
    row = (await conn.execute(_REAP, {"minutes": stale_minutes})).mappings().one()
    return int(row["reaped"])


async def mark_succeeded(
    conn: AsyncConnection, job_id: str, result: dict[str, Any] | None = None
) -> None:
    await conn.execute(_SUCCEED, {"id": job_id, "result": json.dumps(result or {})})


async def mark_failed(
    conn: AsyncConnection, job_id: str, error: str, *, permanent: bool = False
) -> str:
    """Marca el fallo y devuelve el estado resultante.

    Con `permanent=False` vuelve a `queued` con backoff mientras queden intentos
    y recien pasa a `failed` cuando se agotaron: la mayoria de los fallos aca son
    de red. Con `permanent=True` no se reintenta.
    """
    if permanent:
        await conn.execute(_FAIL_PERMANENT, {"id": job_id, "error": error[:500]})
        return "failed"

    row = (await conn.execute(_FAIL, {"id": job_id, "error": error[:500]})).mappings().one()
    return str(row["status"])


async def latest_for_document(conn: AsyncConnection, document_id: str) -> JobStatus | None:
    row = (
        (await conn.execute(_LATEST_FOR_DOCUMENT, {"document_id": document_id}))
        .mappings()
        .one_or_none()
    )
    return JobStatus(**row) if row else None
