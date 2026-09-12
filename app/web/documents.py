"""Pantallas de documentos: lista, subida y detalle.

Es el primer hito usable del proyecto y no toca el LLM. Sirve para lo que
realmente importa en esta fase: subir un resumen real de cada emisor y ver el
texto que sale, que es como se valida el supuesto mas riesgoso —que los PDFs de
homebanking traen capa de texto— antes de gastar un centavo en extraccion.

El estado del job se muestra con polling de HTMX sobre `/documents/{id}/status`,
que devuelve un fragmento y deja de auto-refrescarse cuando el documento llega a
un estado final.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from starlette.responses import RedirectResponse, Response

from app.deps import CurrentProfileDep, CurrentUserDep, SettingsDep
from app.infra import db, storage
from app.jobs import runner as job_runner
from app.logging_config import get_logger
from app.pdf import inspect as pdf_inspect
from app.repositories import document_texts as texts_repo
from app.repositories import documents as documents_repo
from app.repositories import jobs as jobs_repo
from app.repositories import statements as statements_repo
from app.security import csrf
from app.services import ingest
from app.web.templates import render

log = get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


def _wake_runner() -> None:
    """Le avisa al runner que hay trabajo, si este proceso tiene uno."""
    runner = job_runner.get_runner()
    if runner is not None:
        runner.wake()


@router.get("", include_in_schema=False)
async def list_documents(
    request: Request, user: CurrentUserDep, profile: CurrentProfileDep, settings: SettingsDep
) -> Response:
    async with db.user_tx(user.db_claims) as conn:
        documents = await documents_repo.list_recent(conn)

    return render(
        request,
        "documents/list.html",
        {
            "profile": profile,
            "documents": documents,
            "csrf_token": csrf.issue(user.user_id, settings),
        },
    )


@router.get("/upload", include_in_schema=False)
async def upload_form(
    request: Request, user: CurrentUserDep, profile: CurrentProfileDep, settings: SettingsDep
) -> Response:
    return render(
        request,
        "documents/upload.html",
        {
            "profile": profile,
            "csrf_token": csrf.issue(user.user_id, settings),
            "max_mb": settings.max_upload_bytes // (1024 * 1024),
        },
    )


@router.post("", include_in_schema=False)
async def upload(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    csrf_token: Annotated[str, Form()] = "",
    pdf_password: Annotated[str, Form()] = "",
) -> Response:
    """Recibe el PDF y arranca la etapa 1.

    Los errores vuelven al mismo formulario con el motivo. El caso de la
    contraseña es el mas importante de todos: Galicia, Santander y BBVA mandan
    los resumenes cifrados con el DNI, asi que "falta la contraseña" no es un
    error raro sino un paso normal del flujo, y la pantalla lo trata como tal.
    """
    csrf.verify(csrf_token, user.user_id, settings)

    data = await file.read()
    filename = file.filename or "resumen.pdf"

    def _error(message: str, *, ask_password: bool = False, status: int = 400) -> Response:
        return render(
            request,
            "documents/upload.html",
            {
                "profile": profile,
                "csrf_token": csrf.issue(user.user_id, settings),
                "max_mb": settings.max_upload_bytes // (1024 * 1024),
                "error": message,
                "ask_password": ask_password,
                "filename": filename,
            },
            status_code=status,
        )

    if not data:
        return _error("No llego ningun archivo.")

    try:
        result = await ingest.ingest_pdf(
            claims=user.db_claims,
            user_id=user.user_id,
            access_token=user.access_token,
            filename=filename,
            data=data,
            password=pdf_password or None,
            card_id=None,
            settings=settings,
        )
    except pdf_inspect.PasswordRequiredError as exc:
        # 409 y no 400: el archivo esta bien, falta un dato. Es la distincion que
        # deja la puerta abierta a reintentar con la contraseña.
        return _error(exc.message, ask_password=True, status=409)
    except pdf_inspect.WrongPasswordError as exc:
        return _error(exc.message, ask_password=True, status=409)
    except pdf_inspect.PdfError as exc:
        return _error(exc.message)
    except ingest.UploadTooLargeError as exc:
        return _error(exc.message, status=413)
    except storage.StorageError as exc:
        return _error(exc.message, status=502)

    if not result.already_imported:
        _wake_runner()

    # Se redirige igual cuando ya existia: el usuario queria ver ese documento, y
    # "ya lo tenias" se le muestra en el detalle. Un error seria mentir.
    suffix = "?already_imported=1" if result.already_imported else ""
    return RedirectResponse(f"/documents/{result.document.id}{suffix}", status_code=303)


@router.get("/{document_id}", include_in_schema=False)
async def detail(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    document_id: str,
    already_imported: bool = False,
) -> Response:
    async with db.user_tx(user.db_claims) as conn:
        document = await documents_repo.get(conn, document_id)
        if document is None:
            return render(
                request,
                "error.html",
                {
                    "profile": profile,
                    "title": "No existe ese documento",
                    "detail": "O no es tuyo, que para el sistema es lo mismo.",
                    "csrf_token": csrf.issue(user.user_id, settings),
                },
                status_code=404,
            )
        job = await jobs_repo.latest_for_document(conn, document_id)
        extracted = await texts_repo.latest_for_document(conn, document_id)
        # Uno por moneda: un resumen argentino trae pesos y dolares con totales
        # propios, y cada seccion se revisa por separado.
        found = await statements_repo.for_document(conn, document_id)

    return render(
        request,
        "documents/detail.html",
        {
            "profile": profile,
            "document": document,
            "job": job,
            "text": extracted,
            "statements": found,
            "already_imported": already_imported,
            "csrf_token": csrf.issue(user.user_id, settings),
        },
    )


@router.get("/{document_id}/status", include_in_schema=False)
async def status_fragment(request: Request, user: CurrentUserDep, document_id: str) -> Response:
    """Fragmento para el polling de HTMX.

    Devuelve el mismo bloque de estado; cuando el documento llega a un estado
    final, el fragmento sale sin `hx-trigger` y el polling se detiene solo.
    """
    async with db.user_tx(user.db_claims) as conn:
        document = await documents_repo.get(conn, document_id)
        job = await jobs_repo.latest_for_document(conn, document_id) if document else None

    if document is None:
        return Response(status_code=404)

    return render(
        request,
        "partials/document_status.html",
        {"document": document, "job": job},
    )


@router.get("/{document_id}/download", include_in_schema=False)
async def download(
    request: Request, user: CurrentUserDep, settings: SettingsDep, document_id: str
) -> Response:
    """Redirige a una URL firmada de TTL corto.

    El PDF nunca se sirve desde este proceso: la firma la hace Storage con el
    token del usuario, asi que si el documento no fuera suyo, Storage tampoco lo
    dejaria firmarlo.
    """
    async with db.user_tx(user.db_claims) as conn:
        document = await documents_repo.get(conn, document_id)

    if document is None:
        return Response(status_code=404)

    try:
        url = await storage.create_signed_url(
            user.access_token, document.storage_path, settings=settings
        )
    except storage.StorageError as exc:
        return Response(exc.message, status_code=502)

    return RedirectResponse(url, status_code=307)


@router.post("/{document_id}/reprocess", include_in_schema=False)
async def reprocess(
    request: Request,
    user: CurrentUserDep,
    settings: SettingsDep,
    document_id: str,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Vuelve a correr la etapa 1 sobre un documento ya subido.

    Sirve cuando el job fallo por algo transitorio, o despues de mejorar el
    extractor. No vuelve a pedir el PDF: el archivo ya esta guardado descifrado.
    """
    csrf.verify(csrf_token, user.user_id, settings)

    async with db.user_tx(user.db_claims) as conn:
        document = await documents_repo.get(conn, document_id)

    if document is None:
        return Response(status_code=404)

    try:
        download_url = await storage.create_signed_url(
            user.access_token,
            document.storage_path,
            expires_in=storage.JOB_TTL_SECONDS,
            settings=settings,
        )
    except storage.StorageError as exc:
        return Response(exc.message, status_code=502)

    async with db.user_tx(user.db_claims) as conn:
        await documents_repo.set_status(conn, document_id, "uploaded", failure_reason=None)
        # Si ya hay uno activo, `enqueue` devuelve None y no se duplica el trabajo.
        await jobs_repo.enqueue(
            conn,
            document_id=document_id,
            job_type=jobs_repo.EXTRACT_TEXT,
            payload={"download_url": download_url},
            max_attempts=settings.job_max_attempts,
        )

    _wake_runner()
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.post("/{document_id}/reparse", include_in_schema=False)
async def reparse(
    request: Request,
    user: CurrentUserDep,
    settings: SettingsDep,
    document_id: str,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Vuelve a correr SOLO la etapa 2, sobre el texto ya guardado.

    Es distinto de `reprocess`, y la diferencia es el punto del diseño en dos
    etapas: mejorar un prompt no requiere volver a bajar el PDF de Storage ni
    reprocesarlo con pdfplumber. Se relee `document_texts` y listo.

    Lo que ya revisaste a mano no se pierde: la reconciliacion actualiza lo que
    escribio el modelo y conserva lo que tocaste vos (ver
    `repositories/transactions.py`).
    """
    csrf.verify(csrf_token, user.user_id, settings)

    async with db.user_tx(user.db_claims) as conn:
        document = await documents_repo.get(conn, document_id)
        if document is None:
            return Response(status_code=404)

        extracted = await texts_repo.latest_for_document(conn, document_id)
        if extracted is None or not extracted.has_text_layer:
            # Sin texto no hay nada que reinterpretar: lo que hace falta es la
            # etapa 1, y encolar la 2 solo dejaria un job fallando en loop.
            return Response("el documento todavia no tiene texto extraido", status_code=409)

        # Si ya hay uno activo, `enqueue` devuelve None: dos clicks no son dos
        # corridas de LLM pagadas.
        await jobs_repo.enqueue(
            conn,
            document_id=document_id,
            job_type=jobs_repo.PARSE_STATEMENT,
            max_attempts=settings.job_max_attempts,
        )

    _wake_runner()
    return RedirectResponse(f"/documents/{document_id}", status_code=303)
