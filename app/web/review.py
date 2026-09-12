"""La pantalla de revision: `/statements/{id}/review`.

# Por que es la pantalla mas importante del proyecto

Es la que convierte "el sistema extrajo transacciones" en "los numeros del
dashboard son ciertos". Todo lo demas se puede rehacer; un mes confirmado con un
monto mal leido contamina el historico y nadie se entera.

# El patron de HTMX que se usa acá, y por que este y no otro

Cada accion (editar una fila, confirmar en bloque, dividir, agregar) devuelve el
**panel entero** —banner de cuadre + acciones + tabla— y no solo la fila tocada.

Es mas bytes por request y es a proposito: el banner de cuadre depende de la suma
de todas las filas, asi que devolver solo la fila editada dejaria el delta viejo
en pantalla. Un delta desactualizado en la pantalla que existe para verificar
numeros es exactamente el bug que esta fase tiene que no tener. Con ~200 filas y
un usuario, el costo del re-render no se nota.

Sin JavaScript la pantalla igual funciona: los mismos endpoints responden con un
redirect 303 a la pagina completa cuando la request no viene de HTMX.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from app.deps import CurrentProfileDep, CurrentUserDep, SettingsDep
from app.infra import db
from app.logging_config import get_logger
from app.repositories import documents as documents_repo
from app.security import csrf
from app.services import review as review_service
from app.web.templates import is_htmx, render

log = get_logger(__name__)

router = APIRouter(prefix="/statements", tags=["review"])


# ─────────────────────────────────────────────────────────────────────────────
# La pagina
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/{statement_id}/review", include_in_schema=False)
async def review_page(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
) -> Response:
    async with db.user_tx(user.db_claims) as conn:
        state = await review_service.load(conn, statement_id, settings)
        document = await documents_repo.get(conn, state.statement.document_id) if state else None

    if state is None:
        return _not_found(request, profile, user, settings)

    return render(
        request,
        "review/page.html",
        _context(request, state, document, profile, user, settings),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Las mutaciones
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/{statement_id}/transactions/{transaction_id}", include_in_schema=False)
async def edit_transaction(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    transaction_id: str,
) -> Response:
    """Edicion inline de una fila."""
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.edit_transaction(
            conn, transaction_id, _fields(form, review_service.EDITABLE_FIELDS)
        ),
    )


@router.post("/{statement_id}/bulk", include_in_schema=False)
async def bulk(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
) -> Response:
    """Confirmar / rechazar / reabrir varias de una.

    Sin seleccion y con `action=confirm`, confirma todo lo pendiente: es el
    camino normal, en el que se corrigen las pocas que estan mal y se acepta el
    resto en bloque.
    """
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    action = str(form.get("action", ""))
    ids = [str(value) for value in form.getlist("transaction_ids")]

    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.bulk_review(
            conn, statement_id, transaction_ids=ids, action=action
        ),
    )


@router.post("/{statement_id}/transactions", include_in_schema=False)
async def add_transaction(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
) -> Response:
    """Agrega a mano lo que el modelo no vio."""
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    fields = _fields(form, {"posted_date", "description_raw", "amount", "direction", "kind"})
    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.add_transaction(conn, statement_id, fields),
    )


@router.post("/{statement_id}/transactions/{transaction_id}/split", include_in_schema=False)
async def split_transaction(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    transaction_id: str,
) -> Response:
    """Divide una linea en varias conservando el total."""
    form = await request.form()
    csrf.verify(_csrf_of(form), user.user_id, settings)

    amounts = [str(value) for value in form.getlist("part_amount") if str(value).strip()]
    descriptions = [str(value) for value in form.getlist("part_description")]
    parts: list[dict[str, Any]] = [
        {
            "amount": amount,
            "description_raw": descriptions[index] if index < len(descriptions) else "",
        }
        for index, amount in enumerate(amounts)
    ]

    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.split_transaction(conn, transaction_id, parts),
    )


@router.post("/{statement_id}/confirm", include_in_schema=False)
async def confirm(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    csrf_token: Annotated[str, Form()] = "",
    accept_delta: Annotated[str, Form()] = "",
) -> Response:
    """La unica puerta al dashboard."""
    csrf.verify(csrf_token, user.user_id, settings)

    accepted = accept_delta.strip().lower() in ("1", "true", "on", "yes")
    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.confirm_statement(
            conn, statement_id, settings, accept_delta=accepted
        ),
    )


@router.post("/{statement_id}/reopen", include_in_schema=False)
async def reopen(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Devuelve un resumen confirmado a revision."""
    csrf.verify(csrf_token, user.user_id, settings)

    return await _mutate(
        request,
        user,
        profile,
        settings,
        statement_id,
        lambda conn: review_service.reopen_statement(conn, statement_id, settings),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Interno
# ─────────────────────────────────────────────────────────────────────────────
async def _mutate(
    request: Request,
    user: CurrentUserDep,
    profile: CurrentProfileDep,
    settings: SettingsDep,
    statement_id: str,
    operation: Any,
) -> Response:
    """Ejecuta la operacion y devuelve el panel actualizado.

    La operacion y la relectura del estado van en la **misma** transaccion: si
    se leyera despues, con otra conexion, el banner podria mostrar un total de
    antes del cambio que se acaba de hacer.

    Un `ReviewError` no es un 500: es una respuesta al usuario. Se re-renderiza
    el panel con el mensaje arriba y los datos como quedaron.
    """
    error: str | None = None

    async with db.user_tx(user.db_claims) as conn:
        try:
            await operation(conn)
        except review_service.ReviewError as exc:
            error = exc.message
            log.info("accion de revision rechazada", code=exc.code, detail=exc.message)

        state = await review_service.load(conn, statement_id, settings)
        document = await documents_repo.get(conn, state.statement.document_id) if state else None

    if state is None:
        return _not_found(request, profile, user, settings)

    context = _context(request, state, document, profile, user, settings)
    context["error"] = error

    if is_htmx(request):
        return render(request, "review/_panel.html", context)

    if error:
        # Sin HTMX se re-renderiza la pagina entera para no perder el mensaje en
        # un redirect.
        return render(request, "review/page.html", context, status_code=400)

    return RedirectResponse(f"/statements/{statement_id}/review", status_code=303)


def _context(
    request: Request,
    state: review_service.ReviewState,
    document: documents_repo.Document | None,
    profile: CurrentProfileDep,
    user: CurrentUserDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    return {
        "profile": profile,
        "state": state,
        "statement": state.statement,
        "document": document,
        "kinds": sorted(review_service.KINDS),
        "csrf_token": csrf.issue(user.user_id, settings),
    }


def _not_found(
    request: Request, profile: CurrentProfileDep, user: CurrentUserDep, settings: SettingsDep
) -> Response:
    return render(
        request,
        "error.html",
        {
            "profile": profile,
            "title": "No existe ese resumen",
            "detail": "O no es tuyo, que para el sistema es lo mismo.",
            "csrf_token": csrf.issue(user.user_id, settings),
        },
        status_code=404,
    )


def _csrf_of(form: FormData) -> str:
    return str(form.get("csrf_token", ""))


def _fields(form: FormData, allowed: frozenset[str] | set[str]) -> dict[str, Any]:
    """Solo los campos permitidos que el formulario realmente mando.

    Que un campo ausente signifique "no lo cambies" (y no "poneme el default") es
    lo que permite mandar una fila entera o un solo campo con el mismo endpoint.
    """
    return {key: form[key] for key in allowed if key in form}
