"""Subida de un resumen: de los bytes del navegador a un job encolado.

# El orden de los pasos no es arbitrario

    hash -> ¿ya existe? -> validar/descifrar -> Storage -> fila -> job

1. **El hash primero**, sobre los bytes *tal como los subio el usuario*. Es la
   idempotencia: volver a subir el mismo resumen (aunque renombrado) devuelve el
   documento que ya existe en lugar de crear un duplicado y, mas adelante, gastar
   una segunda corrida de LLM. Se hashea el original y no el normalizado porque
   la normalizacion depende de la version de pikepdf: el mismo archivo podria dar
   otro hash despues de actualizar una dependencia.

2. **Storage antes que la fila.** Al reves, un fallo al subir dejaria una fila
   apuntando a un objeto que no existe, y el job fallaria en loop. Si falla el
   insert despues de subir, se borra el objeto: un huerfano en Storage es basura,
   una fila rota es un documento que no se puede procesar ni borrar desde la UI.

3. **El job al final**, en la misma transaccion que la fila, para que no exista
   un job apuntando a un documento que no llego a insertarse.

# La contraseña

No se persiste en ningun lado: se usa para descifrar y se descarta. Lo que va a
Storage es el PDF ya descifrado, con `was_encrypted = true` en la fila. Sin eso,
cada reproceso tendria que volver a pedirsela al usuario.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from app.infra import db, storage
from app.logging_config import get_logger
from app.pdf import inspect as pdf_inspect
from app.repositories import documents as documents_repo
from app.repositories import jobs as jobs_repo
from app.settings import Settings

log = get_logger(__name__)


class UploadTooLargeError(Exception):
    def __init__(self, limit_bytes: int) -> None:
        mb = limit_bytes // (1024 * 1024)
        super().__init__(f"el archivo supera el maximo de {mb} MB")
        self.message = f"el archivo supera el maximo de {mb} MB"


@dataclass(frozen=True)
class IngestResult:
    document: documents_repo.Document
    already_imported: bool
    job_id: str | None


async def ingest_pdf(
    *,
    claims: dict[str, object],
    user_id: str,
    access_token: str,
    filename: str,
    data: bytes,
    password: str | None,
    card_id: str | None,
    settings: Settings,
) -> IngestResult:
    """Ejecuta la etapa de ingesta completa. Levanta `PdfError` si el PDF no sirve."""
    if len(data) > settings.max_upload_bytes:
        raise UploadTooLargeError(settings.max_upload_bytes)

    sha256 = hashlib.sha256(data).hexdigest()

    async with db.user_tx(claims) as conn:
        existing = await documents_repo.get_by_sha256(conn, sha256)
    if existing is not None:
        log.info("documento ya importado", document_id=existing.id)
        return IngestResult(document=existing, already_imported=True, job_id=None)

    # pikepdf es CPU-bound: en el event loop bloquearia a los demas usuarios.
    normalized = await asyncio.to_thread(
        pdf_inspect.normalize, data, password=password, max_pages=settings.max_pdf_pages
    )

    path = storage.object_path(user_id, datetime.now(UTC).year, sha256)
    await storage.upload_pdf(access_token, path, normalized.data, settings=settings)

    try:
        # La URL firmada viaja en el payload del job porque el worker no tiene el
        # JWT del usuario y no se le va a dar la service_role key para leer
        # Storage. Vive en `processing_jobs`, que tambien esta bajo RLS, y su TTL
        # cubre la cola, los reintentos con backoff y una pasada del reaper.
        download_url = await storage.create_signed_url(
            access_token, path, expires_in=storage.JOB_TTL_SECONDS, settings=settings
        )

        async with db.user_tx(claims) as conn:
            document = await documents_repo.create(
                conn,
                storage_path=path,
                original_filename=filename,
                byte_size=len(normalized.data),
                sha256=sha256,
                page_count=normalized.page_count,
                was_encrypted=normalized.was_encrypted,
                card_id=card_id,
            )
            job_id = await jobs_repo.enqueue(
                conn,
                document_id=document.id,
                job_type=jobs_repo.EXTRACT_TEXT,
                payload={"download_url": download_url},
                max_attempts=settings.job_max_attempts,
            )
    except Exception:
        await storage.remove(access_token, path, settings=settings)
        raise

    log.info(
        "documento ingerido",
        document_id=document.id,
        pages=normalized.page_count,
        was_encrypted=normalized.was_encrypted,
    )
    return IngestResult(document=document, already_imported=False, job_id=job_id)
