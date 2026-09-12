"""Handlers de los jobs. Uno por `job_type`.

Todos reciben un `ClaimedJob` y corren con `system_tx(job.user_id)`: el mismo rol
`app_runtime` y RLS activo, con la identidad del dueño del job sintetizada. Un
handler con un bug no puede escribir en los datos de otro usuario — no porque se
acuerde de filtrar, sino porque Postgres no se lo permite.

Dos clases de fallo, y la diferencia importa:

- **`PermanentJobError`** (el PDF no tiene capa de texto, falta la API key, el
  usuario agoto el presupuesto del mes): reintentar da lo mismo. Se marca el
  documento como `failed` con el motivo y el job no vuelve a la cola.
- **Cualquier otra excepcion**: se asume transitoria (Storage caido, la URL
  firmada vencio, el modelo devolvio un 429) y el runner la reencola con backoff
  hasta agotar los intentos.

# Por que la etapa 2 es un job aparte y no la cola de la etapa 1

`extract_text` encola `parse_statement` en vez de hacerlo el mismo. Asi, mejorar
un prompt es reencolar solo la etapa 2 sobre los documentos que ya tienen texto,
sin volver a bajar ni reprocesar un PDF. Y si el LLM esta caido, el texto —que es
lo caro de obtener— ya quedo guardado.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.infra import db, storage
from app.llm import factory as llm_factory
from app.llm.ports import LLMBudgetExceededError, LLMConfigError
from app.logging_config import get_logger
from app.pdf import extract as pdf_extract
from app.repositories import document_texts as texts_repo
from app.repositories import documents as documents_repo
from app.repositories import jobs as jobs_repo
from app.repositories import statements as statements_repo
from app.services import enrich as enrich_service
from app.services import parse as parse_service
from app.settings import get_settings

log = get_logger(__name__)


class PermanentJobError(Exception):
    """El job no puede tener exito por mas que se reintente.

    `reason` es lo que queda en la fila del documento, que es lo que la UI le
    muestra al usuario.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


async def extract_text(job: jobs_repo.ClaimedJob) -> dict[str, Any]:
    """Etapa 1: baja el PDF, extrae el texto y lo guarda.

    No usa el LLM ni lo necesita. Es el paso que valida el supuesto mas riesgoso
    del proyecto — que los resumenes traen capa de texto — a costo cero.
    """
    if not job.document_id:
        raise PermanentJobError("el job no apunta a ningun documento", reason="invalid_job")

    download_url = job.payload.get("download_url")
    if not download_url or not isinstance(download_url, str):
        raise PermanentJobError("el job no tiene el link al archivo", reason="missing_download_url")

    data = await storage.download(download_url)

    # pdfplumber es CPU-bound: fuera del event loop.
    extracted = await asyncio.to_thread(pdf_extract.extract, data)

    async with db.system_tx(job.user_id) as conn:
        await texts_repo.upsert(
            conn,
            document_id=job.document_id,
            extractor=extracted.extractor,
            extractor_version=extracted.extractor_version,
            page_texts=extracted.page_texts,
            full_text=extracted.full_text,
            char_count=extracted.char_count,
            has_text_layer=extracted.has_text_layer,
        )

        if not extracted.has_text_layer:
            # Se guarda igual lo poco que haya: sirve para diagnosticar por que
            # se considero un escaneo sin volver a bajar el PDF.
            await documents_repo.set_status(
                conn, job.document_id, "failed", failure_reason="no_text_layer"
            )
        else:
            await documents_repo.set_status(conn, job.document_id, "text_extracted")
            await jobs_repo.enqueue(
                conn,
                document_id=job.document_id,
                job_type=jobs_repo.PARSE_STATEMENT,
                max_attempts=get_settings().job_max_attempts,
            )

    if not extracted.has_text_layer:
        log.warning(
            "documento sin capa de texto",
            document_id=job.document_id,
            char_count=extracted.char_count,
        )
        raise PermanentJobError(
            "el PDF no tiene texto seleccionable (parece un escaneo)", reason="no_text_layer"
        )

    return {
        "pages": extracted.page_count,
        "char_count": extracted.char_count,
        "extractor_version": extracted.extractor_version,
    }


async def parse_statement(job: jobs_repo.ClaimedJob) -> dict[str, Any]:
    """Etapa 2: del texto guardado a las transacciones, con el LLM.

    El documento queda en `parsed` aunque la validacion no cuadre. No cuadrar no
    es un fallo del job: es un resultado que la pantalla de revision sabe
    mostrar, con el delta a la vista. Marcarlo `failed` esconderia transacciones
    que en su mayoria estan bien.
    """
    if not job.document_id:
        raise PermanentJobError("el job no apunta a ningun documento", reason="invalid_job")

    settings = get_settings()

    try:
        extractor = llm_factory.build(settings)
    except LLMConfigError as exc:
        raise PermanentJobError(str(exc), reason="llm_not_configured") from exc

    try:
        outcome = await parse_service.parse_document(
            user_id=job.user_id,
            document_id=job.document_id,
            extractor=extractor,
            settings=settings,
            # Un job reintentado no vuelve a pagar la pasada de reparacion: si
            # el primer intento ya la hizo, el segundo gastaria el doble para
            # llegar al mismo lugar.
            allow_repair=job.attempts <= 1,
        )
    except LLMBudgetExceededError as exc:
        raise PermanentJobError(str(exc), reason="llm_budget_exceeded") from exc
    except parse_service.ParseError as exc:
        raise PermanentJobError(exc.message, reason=exc.reason) from exc
    finally:
        await extractor.aclose()

    # Enriquecer va aparte: mejorar la categorizacion no tiene por que costar
    # una re-extraccion, y si el enriquecimiento falla las transacciones ya
    # estan guardadas y revisables.
    if outcome.statement_ids:
        async with db.system_tx(job.user_id) as conn:
            await jobs_repo.enqueue(
                conn,
                document_id=job.document_id,
                job_type=jobs_repo.ENRICH,
                max_attempts=settings.job_max_attempts,
            )

    return outcome.summary()


async def enrich(job: jobs_repo.ClaimedJob) -> dict[str, Any]:
    """Fase 5: comercio y categoria para las transacciones del documento.

    Corre sobre todos los resumenes del documento (uno por moneda). El grueso se
    resuelve sin red —por tipo de movimiento, por la memoria de alias y por
    reglas— y solo lo que sobra llega al modelo, en un batch.
    """
    if not job.document_id:
        raise PermanentJobError("el job no apunta a ningun documento", reason="invalid_job")

    settings = get_settings()

    try:
        extractor = llm_factory.build(settings)
    except LLMConfigError as exc:
        raise PermanentJobError(str(exc), reason="llm_not_configured") from exc

    async with db.system_tx(job.user_id) as conn:
        found = await statements_repo.for_document(conn, job.document_id)

    totals: dict[str, Any] = {"statements": len(found), "by_llm": 0, "cost_usd": "0"}
    try:
        for statement in found:
            outcome = await enrich_service.enrich_statement(
                user_id=job.user_id,
                statement_id=statement.id,
                extractor=extractor,
                settings=settings,
            )
            totals["by_llm"] += outcome.by_llm
            totals[f"{statement.currency}"] = outcome.summary()
    except LLMBudgetExceededError as exc:
        raise PermanentJobError(str(exc), reason="llm_budget_exceeded") from exc
    except enrich_service.EnrichError as exc:
        raise PermanentJobError(exc.message, reason=exc.reason) from exc
    finally:
        await extractor.aclose()

    return totals


# El registro que consulta el runner. Agregar un tipo de job es agregar una
# entrada aca; si falta, el runner lo marca fallido en vez de ignorarlo en
# silencio.
HANDLERS: dict[str, Any] = {
    jobs_repo.EXTRACT_TEXT: extract_text,
    jobs_repo.PARSE_STATEMENT: parse_statement,
    jobs_repo.ENRICH: enrich,
}
